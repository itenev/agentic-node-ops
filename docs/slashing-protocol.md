# Slashing Protocol

## Detection Surface

| Signal | Source | Latency |
|---|---|---|
| Duplicate validator index seen | VC logs | seconds |
| Second VC container running | Docker socket | real-time |
| Slashing protection DB error | VC logs | seconds |
| Clock skew > 500ms | NTP / system | seconds |
| Double vote seen on beacon chain | Beacon REST API | ~12s slot |
| External slasher alert | Prometheus exporter | variable |

## Detection Rules

```yaml
- alert: ValidatorDoubleInstance
  expr: count(docker_container_running{name=~"validator.*"}) > 1
  for: 10s
  severity: critical

- alert: SlashingProtectionDBError
  # Loki ruler rule: exact string match (|= not |~) for literal phrases.
  # No `for` clause — fires on first Loki ruler evaluation after the log line appears.
  expr: sum(count_over_time({container="validator"} |= "slashing protection" |= "error" [1m])) > 0
  severity: critical

- alert: ClockSkewExcessive
  expr: node_timex_offset_seconds > 0.5 or node_timex_offset_seconds < -0.5
  for: 30s
  severity: high
```

**Note on `SlashingProtectionDBError`:** No `for` clause is intentional. A slashing protection DB error must fire on the first Loki ruler evaluation after the log line appears — a `for` confirmation window is a risk window in which the validator could act on a corrupted or absent protection file. Actual latency is bounded by the Loki ruler evaluation interval (typically 15-30s), not by any Prometheus-style `for` delay. Use `|=` (exact string filter) not `|~` (regex) for literal phrase matching — it is faster and unambiguous.

## Hermes Response Protocol

### Step 1 — IMMEDIATE (< 5 seconds)

1. Suspend normal queue processing — slashing is priority 0
2. Page operator via ALL configured channels simultaneously (Discord + ntfy urgent)
3. Do NOT attempt any remediation
4. Snapshot: `docker ps`, last 500 lines of VC logs, slashing protection DB copy, beacon validator status, system clock offset

### Step 2 — FORENSIC CONTEXT (< 30 seconds)

5. Query beacon API for recent attestation history for all managed pubkeys
6. Check for proposals in the last 2 epochs
7. Identify if a second VC process is running (docker ps + /proc scan)
8. Confirm slashing protection DB integrity

### Step 3 — OPERATOR SUMMARY

9. Send structured notification with findings, recommended action (if any), and explicit warning not to restart without confirming which DB is correct

### Step 4 — EVIDENCE PRESERVATION

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

## What Hermes Must NEVER Do for Slashing

- Auto-restart any validator container
- Auto-stop a validator without explicit operator approval
- Modify the slashing protection database
- Assume the "newer" container is the wrong one
- Defer or queue — slashing always jumps the queue

---

## Related Documents

- [Architecture](architecture.md) — system design, deployment
- [Webhook Receiver Spec](webhook-receiver-spec.md) — alert ingestion, dedup
- [Notification Design](notification-design.md) — Discord + ntfy routing
