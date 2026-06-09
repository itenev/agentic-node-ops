# Copilot instructions for agentic-node-ops

Purpose: give future Copilot sessions the minimal, concrete repo-specific guidance needed to make correct changes and run tests.

---

## Quick dev commands

- Python version: 3.12+ (project requires >=3.12).
- Install dev deps (editable):
  python3 -m venv .venv
  .venv/bin/pip install -e ".[dev]"

- Run full tests (local):
  .venv/bin/pytest tests/ -v

- Run a single test module or test:
  .venv/bin/pytest tests/test_notifications.py -v
  .venv/bin/pytest tests/test_notifications.py::test_some_case -q

- Lint / format checks (same as CI):
  ruff check .
  ruff format --check .

- Build a wheel:
  python3 -m pip install build
  python3 -m build

- CI coverage command used in workflow:
  pytest -v --cov=agentic_node_ops --cov=webhook_receiver --cov-report=term-missing

---

## High-level architecture (concise)

- Telemetry and detection are external (Prometheus + Alertmanager). This repository provides the "reasoning" and notification layers.
- Two main runtime pieces:
  - webhook-receiver/ : aiohttp-based HTTP receiver that normalizes incoming alerts and appends jsonl lines to alerts.jsonl
  - Hermes side (src/agentic_node_ops/): reads alerts.jsonl, enriches with context, writes incidents to SQLite, matches runbooks, and dispatches notifications.

- Major modules:
  - processor.py — jsonl drain loop + offset handling + orchestration
  - dispatcher.py — routes NotificationPayloads to channels (Discord, ntfy)
  - context.py — builds Hermes prompt from DB history, baselines, and diagnostics
  - runbooks.py — YAML runbook loader & matcher (triggers, diagnostics, suggested_actions, privileged_actions)
  - database.py — SQLite WAL wrapper; single-writer boundary
  - types.py — NotificationPayload, Severity, CHANNEL_ROUTING, NTFY priority mappings

- Data flow (high-level):
  Alertmanager -> webhook-receiver -> alerts.jsonl (append-only) -> processor reads jsonl -> build context -> db.insert_incident -> dispatcher dispatches notifications

- Single-writer boundaries & offsets:
  - webhook receiver is the only writer of alerts.jsonl
  - Hermes side is the sole writer of the SQLite incident DB
  - Processor uses ALERTS_JSONL_PATH and ALERT_OFFSET_PATH (defaults: /var/hermes/alerts.jsonl and /var/hermes/alerts.jsonl.offset). Offsets are written atomically.

---

## Key conventions & repo-specific rules

- Location of runbooks: runbooks/*.yaml. Each runbook YAML may contain: id, triggers (alert_type, min_severity), diagnostics, suggested_actions, privileged_actions. See runbooks.py dataclasses for exact fields.

- Severity model: low, medium, high, critical. Use types.Severity enum and follow CHANNEL_ROUTING in types.py (discord always; ntfy for critical and slashing risk).

- NotificationPayload is the canonical object passed to dispatcher — build payloads using NotificationPayload fields only (no raw alert dicts) so downstream channels remain stable.

- Dispatcher configuration is read from environment variables at startup; helpers raise if required values are missing:
  - DISCORD_WEBHOOK_URL (required)
  - NTFY_TOPIC (required)
  - NTFY_SERVER_URL, NTFY_USERNAME, NTFY_PASSWORD (optional)

- Always prefer non-destructive edits: runbooks are YAML files — modify cautiously; load_runbooks() skips malformed files and logs a warning.

- Tests and pytest config:
  - Tests live under tests/ and webhook-receiver/tests/.
  - pytest is configured (pyproject) with test paths and pythonpath entries pointing to src and webhook-receiver/src.

- CI expectations:
  - Ruff is used for lint/format checks (ruff check / ruff format --check).
  - pip-audit is run in CI to detect dependency issues.
  - Unit tests run with coverage for both agentic_node_ops and webhook_receiver.

- Error-handling semantics in processor:
  - Malformed JSON lines are logged and skipped; offset advances.
  - sqlite3.IntegrityError on insert (duplicate incident) advances offset to avoid infinite retry.
  - Other DB/write errors do NOT advance offset (intentional: retry behavior).

- Runbook matching: match_runbook(runbooks, alert_type, severity) returns the first runbook whose trigger.alert_type matches and whose min_severity <= alert severity.

---

## Files to consult for code-driven behavior

- src/agentic_node_ops/processor.py — offset semantics and orchestration
- src/agentic_node_ops/dispatcher.py — environment variables and routing
- src/agentic_node_ops/runbooks.py — runbook YAML schema & matching
- src/agentic_node_ops/types.py — routing tables and severity enums
- README.md — design doc, quick start, runbook & deployment guidance
- .github/workflows/python-package.yml — CI commands (ruff, pip-audit, pytest+coverage)

---

If this file exists already, prefer adding missing commands or conventions rather than replacing existing content.

