# Webhook Receiver Specification

## Architecture

The webhook receiver runs as a **separate lightweight process** from the main Hermes agent. This ensures alerts are always accepted even if the LLM inference pipeline is down, blocked, or rate-limited.

```
Alertmanager
    │
    │  POST /webhook  (raw Alertmanager payload)
    │  http://webhook-receiver:8090/webhook
    ▼
┌─────────────────────────────┐
│   Webhook Receiver          │  ← standalone lightweight HTTP process
│   (Python http.server /     │     Runs outside the LLM loop
│    aiohttp, < 50MB RAM)     │     Bind-mounted as a Unix socket
│   - validate schema         │     or separate port (default 8090)
│   - deduplicate by fingerprint
│   - normalize to HermesAlert
│   - append to alerts.jsonl  │  ← sole writer; see Queue Design
└────────────┬────────────────┘
             │
             ▼
    ┌─────────────────────────────┐
    │   Alert Queue               │
    │                             │
    │   Primary: alerts.jsonl     │  ← append-only flat file
    │   (webhook-receiver writes) │     survives SQLite unavailability
    │                             │     simple, no concurrent-writer risk
    │   Secondary: SQLite (WAL)   │  ← hermes-agent is SOLE SQLite writer
    │   (hermes-agent writes)     │     drains jsonl → SQLite on each cycle
    └────────┬────────────────────┘
             │
             ▼
    ┌─────────────────────────────┐
    │   Alert Processor (Hermes)  │
    │   - read from alerts.jsonl  │
    │   - write processed records │
    │     to SQLite               │
    │   - load runbook            │
    │   - gather context          │
    │   - reason + summarize      │
    │   - notify operator         │
    │   - write outcome to memory │
    └─────────────────────────────┘
```

## Alertmanager Configuration

```yaml
# alertmanager.yml
route:
  receiver: hermes-webhook
  group_by: ['alertname', 'host']
  group_wait: 10s
  group_interval: 30s
  repeat_interval: 1h

receivers:
  - name: hermes-webhook
    webhook_configs:
      - url: 'http://webhook-receiver:8090/webhook'
        send_resolved: true   # notify Hermes when alerts clear
        http_config: {}       # no auth required on the internal network
```

`send_resolved: true` is required so Hermes can close incidents and trigger post-incident feedback collection when an alert clears.

The receiver is reachable at `http://webhook-receiver:8090/webhook` via Docker DNS — Alertmanager and the webhook-receiver share the `ethd_default` network. No host port mapping is needed in production.

## Queue Design: Single-Writer Boundary

**The webhook receiver never writes to SQLite. Hermes agent is the sole SQLite writer.**

This eliminates concurrent-writer contention entirely:

```
webhook-receiver  →  appends JSON lines to alerts.jsonl        (O_APPEND, atomic per line)
hermes-agent      →  reads jsonl from last_read_offset
                  →  processes alerts one at a time
                  →  writes processed records to SQLite
                  →  updates last_read_offset on each cycle
```

**Offset pointer file:**

```
/var/hermes/alerts.jsonl.offset
```

Plain text file containing a single integer: the byte offset of the last successfully processed line in `alerts.jsonl`. Written atomically via `tempfile + rename` to prevent corruption on crash.

```python
# Read current offset (0 if file absent — start from beginning)
def read_offset(path: str) -> int:
    try:
        return int(Path(path).read_text().strip())
    except FileNotFoundError:
        return 0

# Write new offset atomically
def write_offset(path: str, offset: int) -> None:
    tmp = path + ".tmp"
    Path(tmp).write_text(str(offset))
    os.replace(tmp, path)   # atomic on POSIX
```

The offset is updated **after** a line is successfully written to SQLite, not after it is read. This ensures that if hermes-agent crashes mid-processing, the line is reprocessed on restart rather than silently dropped.

If hermes-agent is down, the receiver continues appending. On restart, the agent resumes from `last_read_offset` and processes the backlog before accepting new alerts.

**Queue rotation safety:** Archive `alerts.jsonl` to `alerts.jsonl.YYYY-MM-DD` and reset the offset to 0 **only when `last_read_offset` equals EOF** (file fully drained). If unprocessed lines exist at the end of the file at rotation time, preserve the tail in the new file and update the offset accordingly. Never discard unprocessed lines.

**Deduplication note:** The webhook receiver needs read-only access to the SQLite `incidents` table for `db.get_last_processed()` lookups (deduplication runs at receive time, before writing to jsonl). Mount the SQLite file as read-only in the receiver container, or open it with `?mode=ro`. The receiver never writes to it — all inserts go through hermes-agent. Initialize the database with `PRAGMA journal_mode=WAL;` to ensure the read-only receiver and writer agent can access the database concurrently without `SQLITE_BUSY` locks.

## Context Snapshot Fetch Behavior

Context is pre-fetched at receive time. If a context source is unavailable (e.g. Lighthouse API is down because it crashed), the receiver substitutes **last-known values from Prometheus** for that metric. If Prometheus is also unreachable, the field is set to `"unavailable"` and flagged in the alert so Hermes knows context is stale.

```json
{
  "context_snapshot": {
    "head_slot_distance": 184,
    "peer_count": 2,
    "container_status": "unreachable",
    "container_status_note": "docker socket returned connection refused, using last Prometheus value: running",
    "validator_count": 3
  }
}
```

`validator_count` represents the number of validator keys loaded and active in the VC process, sourced from the VC metrics endpoint (`validator_count` gauge on Lighthouse, equivalent on other clients). Used by Hermes to contextualise duty-miss severity.

## Deduplication Logic

```python
SEVERITY_ORDER = {"critical": 1, "high": 2, "medium": 3, "low": 4}

COOLDOWN = {
    "critical": timedelta(minutes=15),
    "high":     timedelta(hours=1),
    "medium":   timedelta(hours=4),
}

def should_process(alert, db):
    last = db.get_last_processed(alert.alert_type, alert.host)
    if not last: return True
    if last.status == "resolved" and alert.status == "firing": return True
    if SEVERITY_ORDER.get(alert.severity, 99) < SEVERITY_ORDER.get(last.severity, 99): return True
    if (alert.fired_at - last.processed_at) > COOLDOWN[alert.severity]: return True
    return False
```

## Alert Storm Protection

**Single-host:** If > 3 alerts arrive for the same host within 30 seconds, bundle them as a single "multi-system failure" incident and process once with combined context.

**Cross-host correlation:** If the same alert type fires across >= 2 hosts within a 60-second window, treat it as a potential upstream/network issue. The receiver aggregates these into a single "cluster-wide" incident with per-host context snapshots. This prevents alert fatigue during network partitions, ISP outages, or consensus-layer disruptions affecting multiple validators simultaneously.

## Self-Monitoring (Watchdog)

```yaml
# Hermes emits a heartbeat metric every 60s
- alert: HermesAgentSilent
  expr: absent(hermes_alive) or hermes_alive == 0
  for: 2m
  annotations:
    summary: "Hermes agent not responding — alerts may be missed"

# Webhook receiver health (separate from Hermes process):
- alert: WebhookReceiverDown
  expr: absent(webhook_receiver_up) or webhook_receiver_up == 0
  for: 30s
  annotations:
    summary: "Alertmanager webhook receiver is down — alerts will be queued in jsonl but not processed"
```

## Normalized HermesAlert Schema

```json
{
  "id": "evt_abc123_1704067200",
  "alert_type": "consensus_desync",
  "severity": "critical",
  "status": "firing",
  "client": "lighthouse",
  "host": "validator-01",
  "container": "consensus",
  "fired_at": "2025-01-01T00:00:00Z",
  "context_snapshot": {
    "head_slot_distance": 184,
    "peer_count": 2,
    "container_status": "running",
    "validator_count": 3
  },
  "runbook_hint": "consensus_desync",
  "raw_labels": {}
}
```

**Key principle:** pre-fetch the cheap context snapshot *at receive time*, not when the LLM processes it.

---

## Related Documents

- [Architecture](architecture.md) — system design, deployment, socket-proxy pattern
- [Slashing Protocol](slashing-protocol.md) — slashing detection, response protocol
- [Runbook Spec](runbook-spec.md) — runbook schema, approval model
- [Memory and Feedback](memory-and-feedback.md) — DB schema, feedback loop
- [Alert Set](alert-set.md) — initial alerts, telemetry health
- [Notification Design](notification-design.md) — two-tier routing, Discord, ntfy
