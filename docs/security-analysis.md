# Security Analysis

> Scope: full implementation review of `src/agentic_node_ops/` and `webhook-receiver/src/`, runbooks, Dockerfile/docker-compose, and the nginx socket-proxy filter, performed 2026-11-27 against commit `4ba1da0` (PR #22 merge).
>
> Threat model: the webhook receiver accepts input from the `ethd_default` network and may be exposed beyond the trusted compose bridge (reverse proxy, wider network). All Alertmanager label values are therefore treated as attacker-controlled. Hermes actions (container restarts) are approval-gated; social-engineering an approval is in scope.

## What was already solid

- SQL is 100% parameterized, including the `"-N seconds"` pattern (pre-formatted in Python).
- Single-writer discipline holds: receiver → `alerts.jsonl` only; agent → SQLite only. WAL mode; dedup opens the DB read-only and fails open.
- Offset-after-commit ordering: JSONL offset advances only after successful SQLite insert and dispatch.
- Context/history is queried before the current event is inserted (no self-pollution).
- Raw Docker socket calls send `Connection: close`.
- Executor defaults to `shell=False` with `shlex.split`; timeouts kill the whole process group (SIGTERM → SIGKILL fallback).
- Socket-proxy pattern is real defense in depth: tecnativa proxy (CONTAINERS=1, POST=1) in front of an nginx path filter that allowlists `POST /containers/{name}/restart` only.
- Runbooks are mounted read-only; actions carry risk/reversibility metadata; privileged actions are hard-blocked without explicit unlock.

## Findings and remediation status

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | HIGH | No authentication on POST /webhook — anything on `ethd_default` could forge alerts (fake incidents, notification noise, talked-into approvals once ntfy action buttons land). | **Fixed** — `feat/webhook-auth-token`: shared-secret `X-Hermes-Token` header, SHA-256 digests + `hmac.compare_digest`, fail-closed 503 when unset. |
| 2 | HIGH | `fetch_context_snapshot` (blocking raw-socket + urllib calls, 2s timeouts each) ran on the aiohttp event loop — a single large payload stalled `/health` for minutes (self-amplifying DoS via Alertmanager retries). | **Fixed** — `feat/context-fetch-async`: `asyncio.to_thread` with bounded semaphore, `MAX_ALERTS_PER_REQUEST` cap (413), fetch failures degrade the snapshot instead of dropping the alert. |
| 3 | MEDIUM | Container label spliced unvalidated into a hand-built HTTP/1.1 request to the Docker API — CRLF/space injection could smuggle requests past the nginx filter (which allows all GETs). | **Fixed** — `feat/container-label-validation`: Docker-name charset enforced at the schema boundary; name percent-encoded again at the request path. This is the boundary any future command/URL interpolation must preserve. |
| 4 | MEDIUM | Config drift: compose set `ALERT_QUEUE_PATH` (read by zero code; the receiver worked only because its default path matched), the receiver had no Dockerfile (`build: ./webhook-receiver` could not build), and `DOCKER_HOST` was set on the receiver though it speaks only to a Unix socket that isn't mounted (container status silently degraded to Prometheus fallback). | **Fixed** — `fix/receiver-deploy-config`: env names aligned to code in compose and architecture.md, receiver Dockerfile + pinned `uv.lock`, misleading env removed, fallback documented. |
| 5 | LOW | ntfy topic acts as bearer secret on public ntfy.sh — infrastructure details (hosts, containers, alert types) leave the LAN, and anyone who learns the topic can also publish spoofed push notifications. | Open — use a long random topic now; self-hosted ntfy with auth is the real fix. |
| 6 | LOW | `hash()`-based event IDs (24 bits, `PYTHONHASHSEED`-randomized): collisions under storms hit the incidents PK and the alert is silently skipped. | Open — switch to `secrets.token_hex(8)` / uuid4. |
| 7 | LOW | Poison-pill retry: a non-IntegrityError processing failure loops forever on the same JSONL line (no retry cap, no dead-letter). | Open — add max-attempts + quarantine file. |
| 8 | LOW | Alert label text flows unescaped into Discord/ntfy messages — forged alerts can render arbitrary operator-facing text (phishing vector; bounded once webhook auth lands). | Open — trim/whitelist label text at notification build time. |
| 9 | INFO | Containers run as root (no USER directive). Not a free fix: the shared `hermes-data` volume plus SQLite WAL means naive non-root users break dedup (`-shm` must be writable by readers). Needs a coordinated uid/gid design. | Open — follow-up hardening task. |
| 10 | INFO | Token travels over plain HTTP on the internal network; nginx filter allows all GETs (including `/containers/json` and container inspect with Env) to anything on `hermes-monitoring`. | Open — TLS or trusted segment; acceptable while the network stays closed. |
| 11 | INFO | Malformed-UTF-8 bodies raise `UnicodeDecodeError` → 500 (aiohttp's `request.json()` path) instead of a clean 400; reachable only by authenticated callers after fix #1. | Open — extend the except tuple when next touching the handler. |
| 12 | INFO | ruff 0.17's expanded default rule set (I/UP/PLW) reports 129 pre-existing findings on `main`, so CI lint is red on current tooling. | Open — separate style PR (`ruff check --fix` handles most); security branches were held to a zero-new-findings delta. |

## Residual-risk notes for operators

1. The webhook token is a shared secret, not per-client identity. If more than one Alertmanager feeds the receiver, rotate independently per instance.
2. `WEBHOOK_AUTH_TOKEN` must be set at deploy time — an unset token is fail-closed: alerts will be rejected (503) and logged loudly, but nothing reaches the queue until the operator fixes the env. Watch `auth_rejected` on `/health`.
3. Container-status context in the receiver relies on the Prometheus fallback chain in the current deployment (no Docker socket mounted); this is by design, documented in the spec.
