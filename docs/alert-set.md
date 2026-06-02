# Alert Set

## Initial Alert Set

| Alert | Key Metric/Signal | Hermes Enrichment |
|---|---|---|
| Consensus Desync | `head_slot_distance > threshold` | sync status, peer count, EL connection, logs |
| Validator Duty Misses | `missed_attestations > N` | correlate with CL lag, VC logs, penalty estimate |
| Slashing Risk | duplicate VC, DB error, clock skew | full forensic protocol (see [Slashing Protocol](slashing-protocol.md)) |
| Client Crash | `docker_container_up == 0` | exit code, last logs, crash pattern matching |

## Telemetry Plane Health (Critical Infrastructure)

These alerts monitor the monitoring system itself. If Prometheus or Loki are down, all downstream detection is blind.

| Alert | Signal | Response |
|---|---|---|
| Prometheus Down | `up{job="prometheus"} == 0` | Page immediately — all detection is offline |
| Prometheus Target Down | `up == 0` for any eth-docker target | Alertmanager fires; Hermes enriches with target-specific context |
| Loki Down | `up{job="loki"} == 0` | Log-based detection offline; metric-based detection still works |
| Alertmanager Queue Backlog | `alertmanager_notifications_failed_total` rising | Alerts may be delayed; check webhook receiver health |
| Grafana Dashboard Down | `up{job="grafana"} == 0` | Visualization only — detection unaffected |

---

## Related Documents

- [Slashing Protocol](slashing-protocol.md) — detailed slashing detection rules
- [Webhook Receiver Spec](webhook-receiver-spec.md) — alert ingestion pipeline
- [Architecture](architecture.md) — system design, technology choices
