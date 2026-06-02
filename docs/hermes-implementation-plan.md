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

**Objective:** Build the standalone HTTP receiver that accepts Alertmanager POSTs, validates schema, normalizes to HermesAlert, and appends to jsonl.

**Files:**
- Create: `webhook-receiver/src/__init__.py`
- Create: `webhook-receiver/src/server.py` (aiohttp/http.server HTTP endpoint)
- Create: `webhook-receiver/src/schema.py` (Alertmanager payload validation)
- Create: `webhook-receiver/src/types.py` (HermesAlert dataclass)
- Create: `webhook-receiver/src/queue_writer.py` (jsonl append with O_APPEND)
- Create: `webhook-receiver/tests/test_server.py`
- Create: `webhook-receiver/tests/test_schema.py`
- Create: `webhook-receiver/tests/test_queue.py`
- Create: `webhook-receiver/pyproject.toml`
- Create: `webhook-receiver/Dockerfile`

**Steps:**
1. Create webhook-receiver package structure
2. Implement HermesAlert schema (matching design doc §3)
3. Implement Alertmanager payload parser + validator
4. Implement jsonl queue writer (atomic append)
5. Implement HTTP server on port 8090, POST /webhook
6. Write tests (TDD)
7. Create Dockerfile
8. Verify: `curl -X POST http://localhost:8090/webhook -d '{"alerts":[...]}'`

---

### Task 6: Implement deduplication logic

**Objective:** Add dedup gate at receive time — read-only SQLite lookup to check cooldowns.

**Files:**
- Modify: `webhook-receiver/src/server.py`
- Create: `webhook-receiver/src/dedup.py`

**Steps:**
1. Implement `should_process()` logic from design doc §3
2. Open SQLite in read-only mode for dedup lookups
3. Integrate into receive pipeline
4. Write tests
5. Verify dedup prevents duplicate alerts within cooldown windows

---

### Task 7: Implement alert storm protection

**Objective:** Bundle alerts when >3 for same host in 30s, or same alert across >=2 hosts in 60s.

**Files:**
- Modify: `webhook-receiver/src/server.py`
- Create: `webhook-receiver/src/storm_protection.py`

**Steps:**
1. Implement single-host bundling (>3 alerts/30s/host)
2. Implement cross-host correlation (same alert type across >=2 hosts/60s)
3. Write tests
4. Verify bundling behavior

---

### Task 8: Implement context snapshot fetch

**Objective:** Pre-fetch cheap context (peer count, sync status, container status) at receive time with Prometheus fallback.

**Files:**
- Modify: `webhook-receiver/src/server.py`
- Create: `webhook-receiver/src/context_fetcher.py`

**Steps:**
1. Implement Prometheus queries for peer count, sync status, etc.
2. Implement Docker socket queries for container status
3. Implement fallback chain: primary source → Prometheus last value → "unavailable"
4. Write tests
5. Verify context snapshot is attached to HermesAlert

---

## Phase 2: Hermes Integration + Notifications

### Task 9: Wire dispatcher to Hermes agent loop

**Objective:** Connect the notification dispatcher to the alert processing loop.

**Files:**
- Create: `src/agentic_node_ops/processor.py` (reads jsonl, drains to SQLite, dispatches)
- Create: `src/agentic_node_ops/database.py` (SQLite WAL, sole writer)

**Steps:**
1. Implement SQLite database with schema from design doc §6
2. Implement jsonl offset reader/writer
3. Implement drain loop: read jsonl → process → write SQLite → update offset
4. Wire dispatcher into processor
5. Write tests
6. Verify end-to-end: jsonl alert → processor → notification sent

---

### Task 10: Implement runbook matching

**Objective:** Match incoming alerts to YAML runbooks in `runbooks/`.

**Files:**
- Create: `src/agentic_node_ops/runbooks.py`
- Create: `runbooks/consensus_desync.yaml` (first real runbook)

**Steps:**
1. Implement runbook loader (YAML parsing)
2. Implement matcher (alert_type → runbook)
3. Create first runbook: consensus_desync
4. Write tests
5. Verify matching produces correct runbook_id in payload

---

## Phase 3: Memory + Feedback

### Task 11: Implement incident history queries

**Objective:** Hermes context assembly needs recent incidents, corrections, runbook stats.

**Files:**
- Modify: `src/agentic_node_ops/database.py`
- Create: `src/agentic_node_ops/context.py`

**Steps:**
1. Implement `get_recent_incidents()`, `get_corrections()`, runbook stats queries
2. Implement `build_hermes_context()` from design doc §6
3. Write tests
4. Verify context assembly produces correct prompt text

---

### Task 12: Implement host baseline learning

**Objective:** Nightly job to compute p50/p95 baselines from Prometheus.

**Files:**
- Create: `src/agentic_node_ops/baselines.py`

**Steps:**
1. Implement Prometheus `/api/v1/query_range` poller
2. Compute p50/p95 per metric per host
3. Store in `host_fingerprints` table
4. Write tests
5. Verify baselines are computed and stored

---

## Phase 4: Remediation + Approval

### Task 13: Implement approval state machine

**Files:**
- Create: `src/agentic_node_ops/approval.py`

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
