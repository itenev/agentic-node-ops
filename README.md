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
| Action | Notifications (Telegram/Discord) + approval-gated remediation (future) |

Full design document: [docs/hermes-agent-design.md](docs/hermes-agent-design.md)

## Project Structure

```
agentic-node-ops/
├── docs/                          # Architecture and design docs
│   └── hermes-agent-design.md
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
│   └── Dockerfile
├── docker-compose.monitoring.yml  # Monitoring stack (Prometheus, Loki, Alertmanager)
├── .gitignore
└── README.md
```

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + queue | Design complete |
| 2 | Hermes integration + runbook matching + notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine | Design complete |
| 5 | Runbook synthesis from historical incidents | Design complete |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

## Quick Start

*Not yet implemented — tracking design first.*

1. Review [docs/hermes-agent-design.md](docs/hermes-agent-design.md)
2. Deploy monitoring stack alongside eth-docker
3. Configure Alertmanager webhook → Hermes receiver
4. Import initial runbooks into `runbooks/`

## Design Constraints

- Hermes is the **reasoning layer**, not the monitoring system
- Detection stays deterministic (Prometheus rules, Loki ruler)
- Slashing risk is always priority 0, never autonomously remediated
- All approvals have explicit timeouts — silence means no action
- Docker socket access uses the socket-proxy pattern

## License

MIT
