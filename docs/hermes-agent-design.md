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
        ┌────────────────────────┐   ┌──────────────────┐
        │ Docker / Node APIs     │   │ Telegram/Discord │
        │ journalctl             │   │ Slack / Email    │
        │ beacon REST APIs       │   └──────────────────┘
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
│   - write to alert queue    │
└────────────┬────────────────┘
             │
             ▼
    ┌─────────────────────────────┐
    │   Alert Queue               │  ← SQLite WAL + flat-file fallback buffer
    │   - Primary: SQLite (WAL)   │     If SQLite is locked/unavailable,
    │   - Fallback: append to     │     append JSON lines to /var/hermes/
    │     /var/hermes/alerts.jsonl│     alerts.jsonl. A background reconciler
    │                             │     drains the file into SQLite on recovery.
    └────────┬────────────────────┘
             │
             ▼
    ┌─────────────────────────────┐
    │   Alert Processor (Hermes)  │
    │   - dequeue one at a time   │
    │   - load runbook            │
    │   - gather context          │
    │   - reason + summarize      │
    │   - notify operator         │
    │   - write outcome to memory │
    └─────────────────────────────┘
```

### Context Snapshot Fetch Behavior

Context is pre-fetched at receive time. If a context source is unavailable (e.g. Lighthouse API is down because it crashed), the receiver substitutes **last-known values from Prometheus** for that metric. If Prometheus is also unreachable for that metric, the field is set to `"unavailable"` and flagged in the alert so Hermes knows context is stale.

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
# Prometheus alert:
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
    summary: "Alertmanager webhook receiver is down — alerts will be queued but not processed"
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
  "container": "lighthouse",
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
  # Loki ruler rule: count lines matching "slashing protection" AND "error"
  # in the validator stream over a 1m window
  expr: sum(count_over_time({container="validator"} |~ "slashing protection" |~ "error" [1m])) > 0
  for: 30s
  severity: critical

- alert: ClockSkewExcessive
  expr: node_timex_offset_seconds > 0.5 or node_timex_offset_seconds < -0.5
  for: 30s
  severity: high
```

### Hermes Response Protocol

**Step 1 — IMMEDIATE (< 5 seconds)**
1. Suspend normal queue processing — slashing is priority 0
2. Page operator via ALL configured channels simultaneously
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
    description: "Restart the Lighthouse consensus client container"
    cmd: "docker restart consensus"
    risk: low
    reversible: true
    requires_approval: true
    approval_timeout: 30m
    pre_conditions:
      - "peer_count < 5"
      - "container_status == running"

privileged_actions:                   # TIER 3: locked in Phase 1, require explicit unlock
  - id: restore_from_checkpoint
    description: "Wipe state and re-sync from checkpoint"
    risk: high
    reversible: false
    requires_approval: true
    requires_explicit_unlock: true
    phase: 3_and_above_only
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
| Grafana Dashboard Down | `up{job="grafana"} == 0` | Visualization only — detection unaffected, but operator loses dashboard access |

---

## 8. Deployment Topology

### eth-docker service naming convention

When deployed alongside eth-docker, use the canonical service names that eth-docker defines. This ensures runbook commands target the correct containers.

| Role | eth-docker default service name | Example |
|---|---|---|
| Execution client | `execution` | `execution` (geth), `execution` (nethermind) |
| Consensus client | `consensus` | `consensus` (lighthouse), `consensus` (teku) |
| Validator client | `validator` | `validator` (lighthouse), `validator` (teku) |
| Prometheus | `prometheus` | `prometheus` |
| Grafana | `grafana` | `grafana` |

Runbook commands reference these names directly (e.g. `docker logs execution`, `docker restart consensus`).

### Docker socket access

The default configuration mounts `/var/run/docker.sock:ro` into the Hermes container. **This is a significant attack surface** — even read-only access exposes container metadata, labels, environment variables, and potentially secrets via `docker inspect`.

**Recommended: socket-proxy pattern**

Run a minimal proxy container that only exposes the specific Docker API endpoints Hermes needs:

```yaml
# docker-compose socket-proxy
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CONTAINERS: 1       # Allow /containers/* endpoints
      IMAGES: 0           # Block /images/*
      NETWORKS: 0         # Block /networks/*
      SECRETS: 0          # Block /secrets/*
      POST: 0             # Block all POST requests (read-only)
    networks:
      - hermes-monitoring

  hermes-agent:
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
```

This reduces the attack surface to only container enumeration and inspection — no image management, network manipulation, or secret access.

### Compose configuration

```yaml
# docker-compose (single host, eth-docker conventions)
services:
  execution:       # geth / nethermind (managed by eth-docker)
  consensus:       # lighthouse / teku / prysm
  validator:       # VC
  prometheus:
  grafana:
  loki:
  alertmanager:
  hermes-agent:
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375  # or direct socket mount
      - PROMETHEUS_URL=http://prometheus:9090
      - LOKI_URL=http://loki:3100
      - BEACON_URL=http://consensus:5052
      - NOTIFICATION_WEBHOOK=...
    volumes:
      - ./runbooks:/runbooks:ro
      - hermes-data:/var/hermes
    networks:
      - hermes-monitoring
    depends_on:
      - docker-socket-proxy  # if using socket-proxy
  webhook-receiver:          # separate lightweight process
    build: ./webhook-receiver  # or image: your-registry/webhook-receiver:latest
    ports:
      - "8090:8090"
    volumes:
      - hermes-data:/var/hermes
    environment:
      - ALERT_QUEUE_PATH=/var/hermes/alerts.db
      - PROMETHEUS_URL=http://prometheus:9090
    networks:
      - hermes-monitoring
    depends_on:
      - prometheus           # needs Prometheus for context snapshot fallback
      - docker-socket-proxy  # needs Docker API for container status
    restart: unless-stopped
```

---

## 9. Technology Choices

| Function | Choice |
|---|---|
| Metrics | Prometheus |
| Alerting | Alertmanager |
| Logs | Loki |
| Visualization | Grafana |
| Agent | Hermes Agent (nousresearch/hermes-agent) |
| Runtime | Docker Compose |
| Notifications | Telegram |
| State / Memory | SQLite (WAL mode) |
| Runbooks | YAML |
| Incident Archive | Local filesystem |

---

## 10. Phased Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + queue | Design complete |
| 2 | Hermes integration + runbook matching + operator notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine | Design complete (§5) |
| 5 | Runbook synthesis from historical incidents | Design complete |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

Phases 1-3 are read-only: Hermes receives, analyzes, and notifies. No autonomous actions. Phases 4-5 add the approval-gated runbook execution defined in §5. Phase 6 is exploratory and requires sufficient incident history to train confidence thresholds.

---

## 11. Key Design Constraints

- Hermes is the **reasoning layer**, not the monitoring system
- Detection stays deterministic (Prometheus rules, Loki ruler)
- LLM never touches raw telemetry streams
- Slashing risk is **always** priority 0, never deferred, never autonomously remediated
- All approvals have explicit timeouts — silence means no action
- Every incident, outcome, and correction is persisted
- The system is safe and useful on day 1 with no autonomous action enabled
- Docker socket access uses the **socket-proxy pattern** — Hermes receives only container enumeration/inspection, never image management, network manipulation, or secret access
- The webhook receiver runs as a **separate process** from the main Hermes agent — alerts are accepted even if the LLM pipeline is down
- Telemetry plane health (Prometheus, Loki, Alertmanager) is monitored at the same level as validator alerts — blind detection is worse than no detection
