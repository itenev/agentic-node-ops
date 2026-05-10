# Hermes Agent — Ethereum Node Monitoring: High-Level Design

> Single-operator · Docker/eth-docker · Read-only observability first · Minimal operational complexity

---

## 1. Architectural Intent

The system separates responsibilities into four planes:

| Plane | Responsibility |
|---|---|
| Telemetry Plane | Collect metrics/logs/traces from Ethereum clients |
| Detection Plane | Detect validator-impacting anomalies |
| Reasoning Plane | Hermes contextual analysis + runbook selection |
| Action Plane | Alerting and optional remediation execution |

**Core design principle: Prometheus Detects. Hermes Reasons.**

Avoid having the LLM continuously poll raw telemetry or make first-order detection decisions. Instead:

- Deterministic systems detect known failures
- Hermes performs contextual enrichment, root-cause correlation, operator explanation, and runbook selection

---

## 2. High-Level System Diagram

```
                           ┌──────────────────────────┐
                           │     Ethereum Host        │
                           │  (docker / eth-docker)   │
                           └────────────┬─────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
                    ▼                   ▼                   ▼
         ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
         │ Consensus Node │  │ Execution Node │  │Validator Client│
         │ Lighthouse etc │  │ Geth/Nethermind│  │ VC metrics/logs│
         └──────┬─────────┘  └──────┬─────────┘  └──────┬─────────┘
                └──────────┬────────┴──────────┬─────────┘
                           ▼                   ▼
                 ┌──────────────────┐  ┌──────────────────┐
                 │    Prometheus    │  │       Loki       │
                 │ metrics scraping │  │ structured logs  │
                 └────────┬─────────┘  └────────┬─────────┘
                          └──────────┬──────────┘
                                     ▼
                         ┌────────────────────┐
                         │   Alertmanager     │
                         │ alert correlation  │
                         └─────────┬──────────┘
                                   │ webhook
                                   ▼
                     ┌─────────────────────────────┐
                     │      Hermes Agent           │
                     │ anomaly interpreter         │
                     │ runbook selector            │
                     │ context gathering           │
                     │ historical reasoning        │
                     │ operator interaction        │
                     └───────┬───────────┬────────┘
                             │           │
              read-only APIs │           │ notifications
                             ▼           ▼
        ┌────────────────────────┐   ┌──────────────────────┐
        │ Docker / Node APIs     │   │ Tier 1: Discord       │
        │ journalctl             │   │ Tier 2: ntfy.sh       │
        │ beacon REST APIs       │   └──────────────────────┘
        │ execution RPC          │
        └────────────────────────┘

        RUNBOOK EXECUTION (Phase 4+)
                             │
                             ▼
                 ┌───────────────────────┐
                 │ Runbook Executor      │
                 │ guarded remediation   │
                 │ docker restart        │
                 │ safe-mode actions     │
                 │ approval state machine│
                 └───────────────────────┘
```

---

## 3. Webhook Interface — Alertmanager → Hermes

### Architecture

The webhook receiver runs as a **separate lightweight process** from the main Hermes agent. This ensures alerts are always accepted even if the LLM inference pipeline is down, blocked, or rate-limited.

```
Alertmanager
    │
    │  POST /webhook  (raw Alertmanager payload)
    ▼
┌─────────────────────────────┐
│   Webhook Receiver          │  ← standalone lightweight HTTP process
│   (Python http.server /     │     Runs outside the LLM loop
│    aiohttp, < 50MB RAM)     │     Bind-mounted as a Unix socket
│   - validate schema         │     or separate port (default 8090)
│   - deduplicate by fingerprint
│   - normalize to HermesAlert
│   - append to alerts.jsonl  │  ← sole writer; see §3 Queue Design
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

### Queue Design: Single-Writer Boundary

**The webhook receiver never writes to SQLite. Hermes agent is the sole SQLite writer.**

This eliminates concurrent-writer contention entirely:

```
webhook-receiver  →  appends JSON lines to alerts.jsonl  (append-only, O_APPEND atomic)
hermes-agent      →  reads jsonl, processes, writes to SQLite incidents table
                  →  marks processed lines with a pointer file (last_read_offset)
```

The jsonl file is the handoff boundary. No locking, no WAL coordination, no `SQLITE_BUSY` risk. The file grows until compacted — hermes-agent archives processed lines to `alerts.jsonl.YYYY-MM-DD` on a daily schedule.

If hermes-agent is down, the receiver continues appending. On restart, the agent resumes from `last_read_offset` and processes the backlog before accepting new alerts.

**Deduplication note:** The webhook receiver needs read-only access to the SQLite `incidents` table for `db.get_last_processed()` lookups (deduplication runs at receive time, before writing to jsonl). Mount the SQLite file as read-only in the receiver container, or open it with `?mode=ro`. The receiver never writes to it — all inserts go through hermes-agent.

### Context Snapshot Fetch Behavior

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

`validator_count` represents the number of validator keys loaded and active in the VC process, sourced from the VC metrics endpoint (`validator_count` gauge on Lighthouse, equivalent on other clients). Used by Hermes to contextualise duty-miss severity — a single miss on a 1-key VC is different from a 100-key VC.

### Deduplication Logic

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

### Alert Storm Protection

**Single-host:** If > 3 alerts arrive for the same host within 30 seconds, bundle them as a single "multi-system failure" incident and process once with combined context.

**Cross-host correlation:** If the same alert type fires across >= 2 hosts within a 60-second window, treat it as a potential upstream/network issue. The receiver aggregates these into a single "cluster-wide" incident with per-host context snapshots. This prevents alert fatigue during network partitions, ISP outages, or consensus-layer disruptions affecting multiple validators simultaneously.

### Self-Monitoring (Watchdog)

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

### Normalized HermesAlert Schema

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

## 4. Slashing Risk — Detailed Treatment

### Detection Surface

| Signal | Source | Latency |
|---|---|---|
| Duplicate validator index seen | VC logs | seconds |
| Second VC container running | Docker socket | real-time |
| Slashing protection DB error | VC logs | seconds |
| Clock skew > 500ms | NTP / system | seconds |
| Double vote seen on beacon chain | Beacon REST API | ~12s slot |
| External slasher alert | Prometheus exporter | variable |

### Detection Rules

```yaml
- alert: ValidatorDoubleInstance
  expr: count(docker_container_running{name=~"validator.*"}) > 1
  for: 10s
  severity: critical

- alert: SlashingProtectionDBError
  # Loki ruler rule: exact string match (|= not |~) for literal phrases.
  # No `for` clause — fire on first match. Any delay is a risk window.
  expr: sum(count_over_time({container="validator"} |= "slashing protection" |= "error" [1m])) > 0
  severity: critical

- alert: ClockSkewExcessive
  expr: node_timex_offset_seconds > 0.5 or node_timex_offset_seconds < -0.5
  for: 30s
  severity: high
```

**Note on `SlashingProtectionDBError`:** No `for` clause is intentional. A slashing protection DB error must fire on the first Loki ruler evaluation after the log line appears — a `for` confirmation window is a risk window in which the validator could act on a corrupted or absent protection file. Actual latency is bounded by the Loki ruler evaluation interval (typically 15-30s), not by any Prometheus-style `for` delay. Use `|=` (exact string filter) not `|~` (regex) for literal phrase matching — it is faster and unambiguous.

### Hermes Response Protocol

**Step 1 — IMMEDIATE (< 5 seconds)**
1. Suspend normal queue processing — slashing is priority 0
2. Page operator via ALL configured channels simultaneously (Discord + ntfy urgent)
3. Do NOT attempt any remediation
4. Snapshot: `docker ps`, last 500 lines of VC logs, slashing protection DB copy, beacon validator status, system clock offset

**Step 2 — FORENSIC CONTEXT (< 30 seconds)**
5. Query beacon API for recent attestation history for all managed pubkeys
6. Check for proposals in the last 2 epochs
7. Identify if a second VC process is running (docker ps + /proc scan)
8. Confirm slashing protection DB integrity

**Step 3 — OPERATOR SUMMARY**
9. Send structured notification with findings, recommended action (if any), and explicit warning not to restart without confirming which DB is correct

**Step 4 — EVIDENCE PRESERVATION**
10. Write full incident bundle to disk regardless of operator response:

```
/var/hermes/incidents/slash_{timestamp}/
├── docker_ps.txt
├── validator_logs.txt
├── slashing_protection_export.json
├── beacon_validator_status.json
├── hermes_analysis.md
└── raw_alert.json
```

### What Hermes Must NEVER Do for Slashing

- Auto-restart any validator container
- Auto-stop a validator without explicit operator approval
- Modify the slashing protection database
- Assume the "newer" container is the wrong one
- Defer or queue — slashing always jumps the queue

---

## 5. Runbook Approval Model

### Three-Tier Action Classification

```yaml
id: consensus_desync

triggers:
  - alert_type: consensus_desync
  - min_severity: high

diagnostics:                          # TIER 1: always run, no approval, no notification
  - id: fetch_sync_status
    cmd: "curl -s http://consensus:5052/eth/v1/node/syncing"
    timeout: 5s
  - id: fetch_peer_count
    cmd: "curl -s http://consensus:5052/eth/v1/node/peer_count"
  - id: tail_logs
    cmd: "docker logs consensus --tail 100 --since 10m"

suggested_actions:                    # TIER 2: Hermes proposes, operator approves each
  - id: restart_consensus_client
    description: "Restart the consensus client container"
    cmd: "docker restart consensus"
    risk: low
    reversible: true
    requires_approval: true
    approval_timeout: 30m
    pre_conditions:
      - "peer_count < 5"
      - "container_status == running"

privileged_actions:                   # TIER 3: locked in Phases 1-3, require explicit unlock
  - id: restore_from_checkpoint
    description: "Wipe state and re-sync from checkpoint"
    risk: high
    reversible: false
    requires_approval: true
    requires_explicit_unlock: true
    phase: 4_and_above_only
```

### Approval State Machine

```
PROPOSED → APPROVED → EXECUTING → SUCCESS
         ↘ SKIPPED               ↘ FAILED
         ↘ TIMEOUT
```

Every state transition is written to the incident record for full audit trail.

### Approval Fatigue Prevention

```python
# Don't re-propose the same action within cooldown
# Escalate (don't repeat) if operator is unresponsive after 2 timeouts
# Auto-group related proposals arriving within 5 minutes
```

---

## 6. Memory and Feedback Loop

### Database Schema

```sql
CREATE TABLE incidents (
    id                   TEXT PRIMARY KEY,
    alert_type           TEXT NOT NULL,
    host                 TEXT NOT NULL,
    severity             TEXT NOT NULL,
    fired_at             DATETIME,
    resolved_at          DATETIME,
    context_snapshot     JSON,
    hermes_analysis      TEXT,
    runbook_used         TEXT,
    actions_proposed     JSON,
    actions_taken        JSON,
    outcome              TEXT,       -- resolved | escalated | timed_out | skipped
    operator_feedback    TEXT,
    feedback_rating      INTEGER,    -- 1-5
    duration_to_resolve  INTEGER,    -- seconds
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE host_fingerprints (
    host            TEXT NOT NULL,
    metric          TEXT NOT NULL,
    baseline_p50    REAL,
    baseline_p95    REAL,
    last_updated    DATETIME,
    PRIMARY KEY (host, metric)
);

CREATE TABLE operator_corrections (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    host        TEXT NOT NULL,
    correction  TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE runbook_outcomes (
    id              TEXT PRIMARY KEY,
    runbook_id      TEXT NOT NULL,
    host            TEXT NOT NULL,
    action_taken    TEXT NOT NULL,
    outcome         TEXT NOT NULL,  -- resolved | did_not_help
    time_to_resolve INTEGER,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Context Assembly for Hermes Prompts

```python
def build_hermes_context(alert: HermesAlert) -> str:
    return f"""
You are analyzing a {alert.alert_type} alert on {alert.host}.

CURRENT STATE:
{json.dumps(alert.context_snapshot, indent=2)}

RECENT INCIDENT HISTORY (last 5 similar incidents on this host):
{db.get_recent_incidents(alert.alert_type, alert.host, limit=5)}

OPERATOR CORRECTIONS FOR THIS ALERT TYPE ON THIS HOST:
{db.get_corrections(alert.alert_type, alert.host)}

RUNBOOK PERFORMANCE:
Runbook '{alert.runbook_hint}' has resolved this alert type
{runbook_stats.success_rate*100:.0f}% of the time.
Known failure cases: {runbook_stats.failed_cases}

HOST BASELINES:
peer_count normal range: {fingerprint.peer_count_p50} (p50) – {fingerprint.peer_count_p95} (p95)
Current: {alert.context_snapshot.peer_count}

Your job: explain what is likely happening, why, and what the operator should do.
Be specific. If this matches a pattern from history, say so explicitly.
"""
```

### Post-Incident Feedback Collection

After every incident closes, Hermes sends a follow-up:

- Was the diagnosis correct? (Yes / No + correction text)
- Was the action helpful? (Fixed it / Didn't help / Fixed it myself)

Corrections are stored and injected into future prompts for the same host/alert type combination.

### Host Baseline Learning

Run as a nightly scheduled task (Hermes cron job or systemd timer): pull last 7 days of key metrics from Prometheus per host via the Prometheus HTTP API (`/api/v1/query_range`), compute p50/p95, store in `host_fingerprints`. Use host-specific baselines rather than global thresholds when evaluating alerts. The job runs at low priority and does not block alert processing if it fails.

---

## 7. Initial Alert Set

| Alert | Key Metric/Signal | Hermes Enrichment |
|---|---|---|
| Consensus Desync | `head_slot_distance > threshold` | sync status, peer count, EL connection, logs |
| Validator Duty Misses | `missed_attestations > N` | correlate with CL lag, VC logs, penalty estimate |
| Slashing Risk | duplicate VC, DB error, clock skew | full forensic protocol (see §4) |
| Client Crash | `docker_container_up == 0` | exit code, last logs, crash pattern matching |

### Telemetry plane health (critical infrastructure)

These alerts monitor the monitoring system itself. If Prometheus or Loki are down, all downstream detection is blind.

| Alert | Signal | Response |
|---|---|---|
| Prometheus Down | `up{job="prometheus"} == 0` | Page immediately — all detection is offline |
| Prometheus Target Down | `up == 0` for any eth-docker target | Alertmanager fires; Hermes enriches with target-specific context |
| Loki Down | `up{job="loki"} == 0` | Log-based detection offline; metric-based detection still works |
| Alertmanager Queue Backlog | `alertmanager_notifications_failed_total` rising | Alerts may be delayed; check webhook receiver health |
| Grafana Dashboard Down | `up{job="grafana"} == 0` | Visualization only — detection unaffected |

---

## 8. Deployment Topology

### eth-docker service naming convention

When deployed alongside eth-docker, use the canonical service names that eth-docker defines. This ensures runbook commands target the correct containers.

| Role | eth-docker default service name |
|---|---|
| Execution client | `execution` |
| Consensus client | `consensus` |
| Validator client | `validator` |
| Prometheus | `prometheus` |
| Grafana | `grafana` |

Runbook commands reference these names directly (e.g. `docker logs execution`, `docker restart consensus`).

### Docker socket access — socket-proxy pattern

The default configuration mounts `/var/run/docker.sock` into the Hermes container. Even read-only access exposes container metadata, labels, environment variables, and potentially secrets via `docker inspect`. **Use the socket-proxy pattern instead.**

Run a minimal proxy container that only exposes the specific Docker API endpoints Hermes needs:

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CONTAINERS: 1   # Allow /containers/* endpoints (inspect, logs, ps)
      IMAGES: 0       # Block /images/*
      NETWORKS: 0     # Block /networks/*
      SECRETS: 0      # Block /secrets/*
      POST: 0         # Block all POST requests — read-only in Phases 1-3
    networks:
      - hermes-monitoring
```

**Phase 4 socket-proxy migration note:** When the Runbook Executor is introduced (Phase 4), `POST: 0` must be relaxed to allow container restart operations. The `tecnativa/docker-socket-proxy` image only supports coarse toggles — it cannot allowlist individual POST endpoints like `/containers/{name}/restart` while blocking others. Setting `POST: 1` would enable *all* mutations (create, delete, exec, etc.), which is too broad.

Options for Phase 4:

1. **nginx sidecar** — front the socket-proxy with a small nginx container that only proxies `POST /containers/*/restart` and `GET /containers/*`:

```yaml
# Phase 4+ nginx sidecar for path-level filtering
  hermes-api-proxy:
    image: nginx:alpine
    volumes:
      - ./nginx-socket.conf:/etc/nginx/nginx.conf:ro
    ports:
      - "2376:2376"
    networks:
      - hermes-monitoring
    depends_on:
      - docker-socket-proxy
```

```nginx
# nginx-socket.conf — only allow restart POST and any GET
upstream docker_socket {
    server docker-socket-proxy:2375;
}

server {
    listen 2376;
    # Allow only restart POST and any GET
    if ($request_method = POST) {
        set $allowed 0;
        if ($request_uri ~ "^/containers/[^/]+/restart$") {
            set $allowed 1;
        }
        if ($allowed = 0) { return 403; }
    }
    location / { proxy_pass http://docker_socket; }
}
```

2. **Custom sidecar** — write a minimal Go/Python proxy that validates each request path against an allowlist before forwarding to the socket-proxy.

3. **Accept `POST: 1`** — if the risk of an LLM hallucinating a `docker create` or `docker rm` is deemed acceptable for Phase 4, set `POST: 1` with the understanding that the Runbook Executor's approval gates provide the real safety boundary.

The principle is: the narrowest possible write surface, audited explicitly at Phase 4 rollout.

### Compose configuration

```yaml
# docker-compose (single host, eth-docker conventions)
# Note: eth-docker manages its own stack on a separate Docker network.
# This compose defines the monitoring augmentation layer.
# See "Note on eth-docker network integration" below for cross-network connectivity.
services:

  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CONTAINERS: 1
      IMAGES: 0
      NETWORKS: 0
      SECRETS: 0
      POST: 0
    networks:
      - hermes-monitoring
    restart: unless-stopped

  webhook-receiver:
    build: ./webhook-receiver
    ports:
      - "8090:8090"
    volumes:
      - hermes-data:/var/hermes
    environment:
      - ALERT_QUEUE_PATH=/var/hermes/alerts.jsonl
      - PROMETHEUS_URL=http://prometheus:9090
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
    networks:
      - hermes-monitoring
    depends_on:
      docker-socket-proxy:
        condition: service_started
    restart: unless-stopped

  hermes-agent:
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - PROMETHEUS_URL=http://prometheus:9090
      - LOKI_URL=http://loki:3100
      - BEACON_URL=http://consensus:5052
      - DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL}
      - NTFY_TOPIC=${NTFY_TOPIC}
    volumes:
      - ./runbooks:/runbooks:ro
      - hermes-data:/var/hermes
    networks:
      - hermes-monitoring
    depends_on:
      docker-socket-proxy:
        condition: service_started
      webhook-receiver:
        condition: service_started
    restart: unless-stopped

volumes:
  hermes-data:

networks:
  hermes-monitoring:
```

**Note on eth-docker network integration:** eth-docker manages its own Docker network. For the monitoring containers (Prometheus, Loki, Alertmanager) to be reachable by the `hermes-monitoring` network, connect the eth-docker network to it:

```bash
# After starting both stacks
docker network connect eth-docker_default webhook-receiver
docker network connect eth-docker_default hermes-agent
```

Or declare it as external in this compose:

```yaml
networks:
  hermes-monitoring:
  eth-docker_default:
    external: true
```

Then reference it on any service that needs cross-network communication (e.g. `hermes-agent` connecting to `http://prometheus:9090`).

**Note on `depends_on`:** `condition: service_started` controls startup ordering only, not runtime availability. The webhook receiver and hermes-agent are designed to tolerate their dependencies being temporarily unavailable at runtime — the jsonl queue and context fallback mechanisms handle this. Do not use `condition: service_healthy` unless health checks are explicitly configured on each dependency.

The hermes-agent's `depends_on: webhook-receiver` ensures the receiver container is created and started first so the shared `alerts.jsonl` file path is initialized. At runtime, if the receiver crashes and restarts, the agent continues reading from the jsonl file normally — the dependency relationship does not cause the agent to stop.

---

## 9. Notification Design — Two-Tier

| Tier | Channel | Trigger | Purpose |
|---|---|---|---|
| 1 | Discord | All severities | Rich embed with full context, runbook link, diagnostics, approval prompts |
| 2 | ntfy.sh | Critical + slashing only | Urgent push notification bypassing iOS/Android silent mode |

### Severity routing

```
low / medium / high  →  Discord only
critical             →  Discord + ntfy (Priority: urgent)
slashing alerts      →  Discord + ntfy (Priority: urgent), regardless of severity field
                       (meta-category: ValidatorDoubleInstance, SlashingProtectionDBError, ClockSkewExcessive)
```

### Discord

Primary operator interface. Renders rich embeds with severity colour, diagnostics as inline fields, proposed actions, and forensic evidence paths for slashing incidents. Uses `@here` for critical alerts and `@everyone` for slashing alerts. Note: `@here` only notifies online members in Discord — it is a secondary wake-up. ntfy.sh (Tier 2) is the primary wake-up for critical and slashing alerts, as it bypasses mobile Do Not Disturb settings.

**Current implementation:** webhook (send-only). Returns Discord message ID for future edits (marking resolved).

**Phase 4 migration:** swap webhook transport for Bot API to support interaction buttons for approval flow. Payload shape (content + embeds) is identical — only the transport changes. The Bot token must be provisioned before Phase 4 rollout.

### ntfy.sh

Single HTTP POST per critical alert. `Priority: urgent` bypasses iOS Do Not Disturb and Android priority settings. Topic name acts as the shared secret — use a long random string.

```bash
# Example: what ntfy sends for a critical alert
curl -d "Lighthouse is 184 slots behind — validator-01" \
     -H "Title: CONSENSUS DESYNC — validator-01" \
     -H "Priority: urgent" \
     -H "Tags: rotating_light,consensus_desync" \
     https://ntfy.sh/${NTFY_TOPIC}
```

Self-hosted ntfy is supported — set `NTFY_SERVER_URL` to your own instance.

### Environment variables

```bash
DISCORD_WEBHOOK_URL   # required — Discord incoming webhook URL
NTFY_TOPIC            # required — random string, acts as shared secret
NTFY_SERVER_URL       # optional — defaults to https://ntfy.sh
NTFY_USERNAME         # optional — for authenticated self-hosted ntfy
NTFY_PASSWORD         # optional
```

---

## 10. Technology Choices

| Function | Choice |
|---|---|
| Metrics | Prometheus |
| Alerting | Alertmanager |
| Logs | Loki |
| Visualization | Grafana |
| Agent | Hermes Agent (nousresearch/hermes-agent) |
| Runtime | Docker Compose |
| Notifications (Tier 1) | Discord (webhook → Bot in Phase 4) |
| Notifications (Tier 2) | ntfy.sh (critical + slashing only) |
| Alert queue | append-only JSONL file (webhook-receiver writes) |
| State / Memory | SQLite WAL (hermes-agent sole writer) |
| Runbooks | YAML |
| Incident Archive | Local filesystem |

---

## 11. Phased Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + jsonl queue | Design complete |
| 2 | Hermes integration + runbook matching + operator notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine + socket-proxy Phase 4 migration + Discord Bot API migration | Design complete (§5, §8, §9) |
| 5 | Runbook synthesis from historical incidents | Design pending |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

Phases 1-3 are read-only: Hermes receives, analyzes, and notifies. No autonomous actions. Phase 4 adds approval-gated runbook execution and requires the socket-proxy migration described in §8. Phase 5 (runbook synthesis) requires sufficient incident history and operator correction data to be meaningful — design to follow once Phase 3 has been running in production. Phase 6 is exploratory.

---

## 12. Key Design Constraints

- Hermes is the **reasoning layer**, not the monitoring system
- Detection stays deterministic (Prometheus rules, Loki ruler)
- LLM never touches raw telemetry streams
- Slashing risk is **always** priority 0, never deferred, never autonomously remediated
- All approvals have explicit timeouts — silence means no action
- Every incident, outcome, and correction is persisted
- The system is safe and useful on day 1 with no autonomous action enabled
- Docker socket access uses the **socket-proxy pattern** — Hermes receives only container enumeration and inspection; never image management, network manipulation, or secret access
- The **webhook receiver is the sole writer to alerts.jsonl**; hermes-agent is the sole writer to SQLite — no concurrent-writer contention by design
- The webhook receiver runs as a **separate process** from the main Hermes agent — alerts are accepted even if the LLM pipeline is down
- Telemetry plane health (Prometheus, Loki, Alertmanager) is monitored at the same level as validator alerts — blind detection is worse than no detection
- `SlashingProtectionDBError` fires immediately on first log match — no `for` clause, no confirmation window
