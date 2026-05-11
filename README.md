# Agentic Node Ops

Ethereum validator node monitoring powered by **Hermes Agent**.

> **Core principle: Prometheus Detects. Hermes Reasons.**

Deterministic alerting for known failures, LLM-driven contextual analysis for everything else.

## Architecture

| Plane | Responsibility |
|---|---|
| Telemetry | Prometheus metrics + Loki logs from Ethereum clients |
| Detection | Alertmanager rules — deterministic, low-latency |
| Reasoning | Hermes Agent — context gathering, root-cause correlation, runbook selection |
| Action | Notifications (Discord + ntfy.sh) + approval-gated remediation (Phase 4+) |

Full design document: [docs/hermes-agent-design-final.md](docs/hermes-agent-design-final.md)

## Project Structure

```
agentic-node-ops/
├── docs/
│   └── hermes-agent-design-final.md  # Implementation spec
├── src/agentic_node_ops/          # Python source
│   ├── __init__.py
│   ├── types.py                   # Alert schemas, data models
│   ├── dispatcher.py              # Alert routing and processing
│   ├── discord.py                 # Discord notification adapter
│   └── ntfy.py                    # ntfy.sh notification adapter
├── tests/                         # Test suite
│   └── test_notifications.py
├── runbooks/                      # YAML runbooks (Phase 2+)
├── webhook-receiver/              # Standalone HTTP receiver (Phase 1)
└── .gitignore
```

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + jsonl queue | Design complete |
| 2 | Hermes integration + runbook matching + operator notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine + socket-proxy migration + Discord Bot API | Design complete |
| 5 | Runbook synthesis from historical incidents | Design pending |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

## Quick Start

*Implementation has not started — design is finalized and implementation-ready.*

1. Review [docs/hermes-agent-design-final.md](docs/hermes-agent-design-final.md)
2. Deploy monitoring stack alongside eth-docker (Prometheus, Loki, Alertmanager)
3. Configure Alertmanager to POST to `http://webhook-receiver:8090/webhook`
4. Implement webhook receiver (Phase 1) + hermes-agent integration (Phase 2)
5. Import initial runbooks into `runbooks/`

## Design Constraints

- Hermes is the **reasoning layer**, not the monitoring system
- Detection stays deterministic (Prometheus rules, Loki ruler)
- Slashing risk is **always** priority 0, never deferred, never autonomously remediated
- All approvals have explicit timeouts — silence means no action
- Docker socket access uses the **socket-proxy pattern**
- **Single-writer boundary:** webhook receiver writes `alerts.jsonl` only; hermes-agent writes SQLite
- Discord + ntfy.sh two-tier notification routing (Discord for all severities, ntfy for critical/slashing)

## License

MIT
