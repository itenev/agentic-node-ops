# Memory and Feedback Loop

## Database Schema

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

CREATE TABLE action_proposals (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    proposed_at DATETIME NOT NULL,
    outcome     TEXT,               -- approved | skipped | timeout | suppressed
    resolved_at DATETIME,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

`action_proposals` is required by the approval fatigue logic — `db.get_last_proposal()`, `db.count_timeouts()`, and `db.mark_action_suppressed()` all read/write this table.

## Context Assembly for Hermes Prompts

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

## Post-Incident Feedback Collection

After every incident closes, Hermes sends a follow-up:

- Was the diagnosis correct? (Yes / No + correction text)
- Was the action helpful? (Fixed it / Didn't help / Fixed it myself)

Corrections are stored and injected into future prompts for the same host/alert type combination.

## Host Baseline Learning

Run as a nightly scheduled task (Hermes cron job or systemd timer): pull last 7 days of key metrics from Prometheus per host via the Prometheus HTTP API (`/api/v1/query_range`), compute p50/p95, store in `host_fingerprints`. Use host-specific baselines rather than global thresholds when evaluating alerts. The job runs at low priority and does not block alert processing if it fails.

---

## Related Documents

- [Runbook Spec](runbook-spec.md) — runbook schema, approval model
- [Architecture](architecture.md) — system design, SQLite sole-writer constraint
- [Webhook Receiver Spec](webhook-receiver-spec.md) — dedup uses read-only SQLite
