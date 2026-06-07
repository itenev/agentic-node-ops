# Agentic Node Ops — Implementation Plan

**Goal:** Build out the agentic Ethereum node monitoring system from design to production deployment, following the phased roadmap in the design doc.

**Architecture:** Prometheus detects → Alertmanager routes → webhook-receiver normalizes → Hermes reasons → notifications/actions. Two-tier notifications (Discord + ntfy.sh). Single-writer boundary (jsonl queue + SQLite).

**Tech Stack:** Python 3.12, httpx, pytest, Docker Compose, SQLite WAL, eth-docker ecosystem.

---

## Task Status Legend

- [ ] Not started
- [~] In progress
- [x] Complete

---

## Phase 0: Project Scaffolding & Existing Code Fixes

### Task 1: Add pyproject.toml and project structure

[x] Complete — Created `pyproject.toml` (setuptools, httpx, pyyaml, pytest, pytest-asyncio). Installed in `.venv/`. Package importable.

### Task 2: Fix test import paths

[x] Complete — Replaced `hermes.notifications.*` with `agentic_node_ops.*`. All 30 tests pass.

### Task 3: Migrate tests to pytest-asyncio

[x] Complete — Replaced deprecated `asyncio.get_event_loop().run_until_complete()` with `async def` + `await`. `asyncio_mode = "auto"` in pyproject.toml. Added `pythonpath = ["src"]` for src layout discovery. All 30 tests pass clean.

---

### Task 4: Split design doc into focused documents

[x] Complete — Split 945-line `hermes-agent-design-final.md` into index (80 lines) + 6 focused documents: `architecture.md`, `webhook-receiver-spec.md`, `slashing-protocol.md`, `runbook-spec.md`, `memory-and-feedback.md`, `alert-set.md`, `notification-design.md`. Updated all cross-references in README and runbooks README.

---

## Phase 1: Webhook Receiver

### Task 5: Implement webhook receiver — HTTP endpoint + schema validation

[x] Complete — Created `webhook-receiver/` standalone package (aiohttp-based):
  - `types.py`: HermesAlert dataclass, Severity/AlertStatus enums, JSONL serialization
  - `schema.py`: Alertmanager payload validation + normalization (12 tests)
  - `server.py`: POST /webhook + GET /health endpoints, QueueWriter (9 tests)
  - `dedup.py`: Read-only SQLite dedup logic (13 tests)
  - `pyproject.toml`: aiohttp + pytest-asyncio deps
  - 39 tests total, all passing

---

### Task 6: Implement deduplication logic

[x] Complete — Created `webhook-receiver/src/webhook_receiver/dedup.py`:
  - `DedupLookup` class: read-only SQLite accessor for incident lookups
  - `should_process(alert, lookup)` → bool with 5 rules:
    1. No prior incident → always process
    2. Resolved → firing → always process
    3. Higher severity breaks dedup
    4. Cooldown elapsed → process (critical=15m, high=1h, medium=4h)
    5. Otherwise → deduplicate (skip)
  - Fails open: if DB is unavailable, alerts always process
  - Handles timezone-naive and timezone-aware datetimes
  - Integrated into `WebhookHandler` — deduped alerts tracked in health endpoint
  - 18 tests (13 dedup unit + 5 integration tests)

---

### Task 7: Implement alert storm protection

[x] Complete — Created `webhook-receiver/src/webhook_receiver/storm_protection.py`:
  - `StormTracker`: in-memory tracker for detecting alert storms
  - `AlertBundle`: dataclass for bundled alerts with `to_alert()` conversion
  - Single-host storm: >3 alerts per host within 30s → bundle as `storm_single_host`
  - Cross-host storm: same alert type across >=2 hosts within 60s → bundle as `storm_cross_host`
  - Bundled alerts written to JSONL with severity inheritance (critical if any is critical)
  - Automatic counter reset after bundle creation
  - Expired entry cleanup (30s for single-host, 60s for cross-host)
  - Integrated into WebhookHandler — bundled alerts tracked in health endpoint
  - 21 tests (15 unit + 4 integration + 2 AlertBundle tests)

---

### Task 8: Implement context snapshot fetch

[x] Complete — Created `webhook-receiver/src/webhook_receiver/context_fetcher.py`:
  - `_query_prometheus`: Queries Prometheus for instant vector values (2s timeout)
  - `_get_docker_container_status`: Queries Docker socket for container state
  - `fetch_context_snapshot`: Orchestrates fallback chain (Docker → Prometheus → "unavailable")
  - Queries peer count and validator count based on client type (lighthouse, prysm, teku, etc.)
  - Integrated into `WebhookHandler` — context snapshot attached to each `HermesAlert` before processing
  - 10 unit tests covering success, failure, and fallback scenarios

---

## Phase 2: Hermes Integration + Notifications

### Task 9: Wire dispatcher to Hermes agent loop

[x] Complete — Created `src/agentic_node_ops/database.py` and `src/agentic_node_ops/processor.py`:
  - `database.py`: SQLite WAL wrapper, sole writer, implements full schema from design doc (incidents, host_fingerprints, operator_corrections, runbook_outcomes, action_proposals)
  - `processor.py`: Reads `alerts.jsonl` from `last_read_offset`, inserts to SQLite, builds `NotificationPayload`, dispatches via `NotificationDispatcher`, and updates offset atomically (tempfile + rename)
  - 12 new unit tests covering database operations, offset management, payload building, and async processing loop
  - All 42 tests passing (30 parent + 12 new)

---

### Task 10: Implement runbook matching

**Objective:** Match incoming alerts to YAML runbooks in `runbooks/`.

**Files:**
- Create: `src/agentic_node_ops/runbooks.py`
- Create: `tests/test_runbooks.py`
- Existing: `runbooks/consensus_desync.yaml`

[x] Complete — Implemented runbook loader and matcher:
  - `runbooks.py`: YAML parsing with dataclasses (`Runbook`, `RunbookTrigger`, `RunbookAction`, `RunbookDiagnostic`) and `match_runbook()` function.
  - `test_runbooks.py`: 6 tests covering loading (valid, empty, directory) and matching (found, not found, multiple triggers).
  - Webhook receiver already sets `runbook_hint=alert_type`, which correctly maps to the runbook ID.
  - All tests passing.

---

## Phase 3: Memory + Feedback

### Task 11: Implement incident history queries

**Objective:** Hermes context assembly needs recent incidents, corrections, runbook stats.

**Files:**
- Modify: `src/agentic_node_ops/database.py`
- Create: `src/agentic_node_ops/context.py`
- Create: `tests/test_context.py`

[x] Complete — Implemented context assembly and history queries:
  - `database.py`: Added `get_runbook_stats()` and `get_host_baselines()` methods.
  - `context.py`: Implemented `build_hermes_context()` combining current state, recent incidents, operator corrections, runbook performance, and host baselines into a single structured prompt.
  - `test_context.py`: 6 tests covering DB queries and context builder formatting.
  - All tests passing.

---

### Task 11b: Wire context builder into processor loop

**Objective:** Replace placeholder summary in `processor.py` with actual `build_hermes_context()` output.

**Files:**
- Modify: `src/agentic_node_ops/processor.py`
- Modify: `tests/test_processor.py`

[x] Complete — Wired context assembly into the main processing loop:
  - `processor.py`: Imports `build_hermes_context` and calls it after DB insertion to populate `payload.summary` with rich, data-driven context before dispatching.
  - `test_processor.py`: Added `test_process_alerts_async_wires_hermes_context` to verify the summary is correctly populated.
  - All tests passing (129 total).

---

### Task 12: Implement host baseline learning

**Objective:** Nightly job to compute p50/p95 baselines from Prometheus.

**Files:**
- Create: `src/agentic_node_ops/baselines.py`
- Modify: `src/agentic_node_ops/database.py`
- Create: `tests/test_baselines.py`

[x] Complete — Implemented host baseline learning:
  - `database.py`: Added `upsert_host_baseline` method for atomic insert/update of baseline metrics.
  - `baselines.py`: Implemented `_query_prometheus_range` poller, `compute_percentiles` (p50/p95), and `update_host_baselines` orchestrator.
  - `test_baselines.py`: 6 tests covering percentile math (empty, single, even, odd) and DB upsert integration.
  - All tests passing (135 total).

---

## Phase 4: Remediation + Approval

### Task 13: Implement approval state machine

**Objective:** Implement approval state machine and fatigue prevention for action proposals.

**Files:**
- Create: `src/agentic_node_ops/approval.py`
- Modify: `src/agentic_node_ops/database.py`
- Create: `tests/test_approval.py`

[x] Complete — Implemented approval state machine:
  - `database.py`: Added `insert_action_proposal`, `get_last_proposal`, `count_timeouts`, `mark_action_suppressed`, `get_pending_proposals`, and `update_proposal_outcome`.
  - `approval.py`: Implemented `should_propose_action` (cooldown/skip checks), `check_timeout_escalation`, `group_pending_proposals`, `propose_action`, and `resolve_proposal`.
  - `test_approval.py`: 12 tests covering fatigue rules, escalation, and proposal grouping.
  - All tests passing (148 total, 90% coverage).

---

### Task 14: Implement runbook executor

**Files:**
- Create: `src/agentic_node_ops/executor.py`

---

### Task 15: Implement nginx socket-proxy migration

**Files:**
- Create: `webhook-receiver/nginx-docker-filter.conf`
- Modify: compose config for Phase 4

---

## Commit Convention

Use Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`
