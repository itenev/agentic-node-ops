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

Design docs: [docs/](docs/) (architecture, webhook spec, runbook spec, notification design, alert set, memory & feedback, slashing protocol).

## Project Structure

```
agentic-node-ops/
├── docs/                          # Design specs (architecture, webhook, runbooks, etc.)
├── src/agentic_node_ops/          # Python source (Hermes integration & notifications)
│   ├── __init__.py
│   ├── types.py                   # Alert schemas, data models
│   ├── dispatcher.py              # Alert routing and processing
│   ├── discord.py                 # Discord notification adapter
│   ├── ntfy.py                    # ntfy.sh notification adapter
│   ├── database.py                # SQLite WAL wrapper (sole writer for incidents)
│   ├── processor.py               # Async jsonl drain, payload build, dispatch, offset update
│   ├── runbooks.py                # YAML runbook loading and alert_type matching
│   ├── context.py                 # Hermes context assembly (incidents, corrections, baselines)
│   ├── baselines.py               # Nightly host baseline learning (p50/p95 percentiles)
│   ├── approval.py                # Approval state machine and fatigue prevention
│   └── executor.py                # Runbook diagnostics and action execution
├── tests/                         # Test suite (161 tests passing, 90% coverage)
│   ├── test_database.py
│   ├── test_notifications.py
│   ├── test_processor.py
│   ├── test_runbooks.py
│   ├── test_context.py
│   ├── test_baselines.py
│   ├── test_approval.py
│   └── test_executor.py
├── runbooks/                      # YAML runbooks (e.g., consensus_desync.yaml)
├── webhook-receiver/              # Standalone HTTP receiver (Phase 1 complete)
│   ├── src/webhook_receiver/      # aiohttp server, schema validation, dedup, storm protection, context fetch
│   └── tests/                     # Webhook receiver test suite
└── docker-compose.yml             # Phase 4 deployment config with nginx socket-proxy
```

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Project scaffolding, packaging, CI/CD | ✅ Complete |
| 1 | Webhook receiver + alert normalization + dedup + storm protection + context fetch | ✅ Complete |
| 2 | Hermes integration + runbook matching + operator notifications | ✅ Complete |
| 3 | Memory layer + feedback loop + host fingerprints + context assembly | ✅ Complete |
| 4 | Tier 2 suggested actions + approval state machine + runbook executor + nginx socket-proxy migration | ✅ Complete |
| 5 | Runbook synthesis from historical incidents | Design pending |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

## Quick Start

### Prerequisites

- Python 3.12+
- Docker + Docker Compose (for deployment)
- eth-docker running on the target host

### Development Setup

```bash
# Create virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run tests
.venv/bin/pytest tests/ -v

# Run a single test module
.venv/bin/pytest tests/test_notifications.py -v
```

### Building

```bash
# Build a wheel distribution
python3 -m pip install build
python3 -m build
# Output: dist/agentic_node_ops-0.1.0-py3-none-any.whl
```

### Releasing

```bash
# Bump version in pyproject.toml, then:
python3 -m build
twine upload dist/*
```

### Deployment

See implementation phases in [docs/hermes-implementation-plan.md](docs/hermes-implementation-plan.md).

1. Review [docs/architecture.md](docs/architecture.md) for system design and [docs/](docs/) for all design documents
2. Deploy monitoring stack alongside eth-docker (Prometheus, Loki, Alertmanager)
3. Configure Alertmanager to POST to `http://webhook-receiver:8090/webhook`
4. Deploy webhook receiver (Phase 1) + hermes-agent integration (Phase 2)
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
