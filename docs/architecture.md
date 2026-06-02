# Agentic Node Ops — Architecture

> Single-operator · Docker/eth-docker · Read-only observability first · Minimal operational complexity

---

## 1. Architectural Intent

The system separates responsibilities into four planes:

| Plane | Responsibility |
|---|---|
| Telemetry Plane | Collect metrics/logs/traces from Ethereum clients |
| Detection Plane | Detect validator-impacting anomalies |
| Reasoning Plane | Hermes contextual analysis + runbook selection |
| Action Plane | Alerting and optional remediation execution |

**Core design principle: Prometheus Detects. Hermes Reasons.**

Avoid having the LLM continuously poll raw telemetry or make first-order detection decisions. Instead:

- Deterministic systems detect known failures
- Hermes performs contextual enrichment, root-cause correlation, operator explanation, and runbook selection

---

## 2. High-Level System Diagram

```
                           ┌──────────────────────────┐
                           │     Ethereum Host        │
                           │  (docker / eth-docker)   │
                           └────────────┬─────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
                    ▼                   ▼                   ▼
         ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
         │ Consensus Node │  │ Execution Node │  │Validator Client│
         │ Lighthouse etc │  │ Geth/Nethermind│  │ VC metrics/logs│
         └──────┬─────────┘  └──────┬─────────┘  └──────┬─────────┘
                └──────────┬────────┴──────────┬─────────┘
                           ▼                   ▼
                 ┌──────────────────┐  ┌────────────────┐
                 │    Prometheus    │  │       Loki       │
                 │ metrics scraping │  │ structured logs  │
                 └────────┬─────────┘  └────────┬─────────┘
                          └──────────┬──────────┘
                                     ▼
                         ┌────────────────────┐
                         │   Alertmanager     │
                         │ alert correlation  │
                         └─────────┬──────────┘
                                   │ POST /webhook
                                   │ http://webhook-receiver:8090/webhook
                                   ▼
                     ┌─────────────────────────────┐
                     │      Hermes Agent           │
                     │ anomaly interpreter         │
                     │ runbook selector            │
                     │ context gathering           │
                     │ historical reasoning        │
                     │ operator interaction        │
                     └───────┬───────────┬────────┘
                             │           │
              read-only APIs │           │ notifications
                             ▼           ▼
        ┌────────────────────────┐   ┌──────────────────────┐
        │ Docker / Node APIs     │   │ Tier 1: Discord      │
        │ journalctl             │   │ Tier 2: ntfy.sh      │
        │ beacon REST APIs       │   └──────────────────────┘
        │ execution RPC          │
        └────────────────────────┘

        RUNBOOK EXECUTION (Phase 4+)
                             │
                             ▼
                 ┌───────────────────────┐
                 │ Runbook Executor      │
                 │ guarded remediation   │
                 │ docker restart        │
                 │ safe-mode actions     │
                 │ approval state machine│
                 └───────────────────────┘
```

---

## 3. Deployment Topology

### eth-docker service naming convention

| Role | eth-docker default service name |
|---|---|
| Execution client | `execution` |
| Consensus client | `consensus` |
| Validator client | `validator` |
| Prometheus | `prometheus` |
| Grafana | `grafana` |

Runbook commands reference these names directly (e.g. `docker logs execution`, `docker restart consensus`).

### Docker socket access — socket-proxy pattern

The default configuration mounts `/var/run/docker.sock` into the Hermes container. Even read-only access exposes container metadata, labels, environment variables, and potentially secrets via `docker inspect`. **Use the socket-proxy pattern instead.**

Run a minimal proxy container that only exposes the specific Docker API endpoints Hermes needs:

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CONTAINERS: 1   # Allow /containers/* endpoints (inspect, logs, ps)
      IMAGES: 0       # Block /images/*
      NETWORKS: 0     # Block /networks/*
      SECRETS: 0      # Block /secrets/*
      POST: 0         # Block all POST requests — read-only in Phases 1-3
    networks:
      - hermes-monitoring
```

**Phase 4 socket-proxy migration note:** When the Runbook Executor is introduced (Phase 4), `POST: 0` must be relaxed to allow container restart operations. The `tecnativa/docker-socket-proxy` image only supports coarse toggles — it cannot allowlist individual POST endpoints like `/containers/{name}/restart` while blocking others. Setting `POST: 1` would enable *all* mutations (create, delete, exec, etc.), which is too broad.

**Option A — nginx sidecar (recommended)**

Front the socket-proxy with an nginx container that enforces path-level filtering. Use `map` to evaluate method + URI together:

```nginx
# nginx-docker-filter.conf
map "$request_method:$request_uri" $docker_allowed {
    default                                         0;
    "~^GET:"                                        1;
    "~^POST:/containers/[^/]+/restart(\?.*)?$"      1;
}

server {
    listen 2376;

    if ($docker_allowed = 0) {
        return 403;
    }

    location / {
        proxy_pass         http://docker-socket-proxy:2375;
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
    }
}
```

```yaml
# Phase 4: replace Phases 1-3 DOCKER_HOST values
services:
  hermes-docker-proxy:
    image: nginx:alpine
    volumes:
      - ./nginx-docker-filter.conf:/etc/nginx/conf.d/default.conf:ro
    networks:
      - hermes-monitoring
    depends_on:
      - docker-socket-proxy
    restart: unless-stopped

  hermes-agent:
    environment:
      - DOCKER_HOST=tcp://hermes-docker-proxy:2376

  webhook-receiver:
    environment:
      - DOCKER_HOST=tcp://hermes-docker-proxy:2376
```

### Compose configuration (Phases 1-3)

```yaml
# docker-compose.yml
services:

  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CONTAINERS: 1
      IMAGES: 0
      NETWORKS: 0
      SECRETS: 0
      POST: 0         # Phase 1-3: read-only. See §8 for Phase 4 migration.
    networks:
      - hermes-monitoring
    restart: unless-stopped

  webhook-receiver:
    build: ./webhook-receiver
    volumes:
      - hermes-data:/var/hermes
    environment:
      - ALERT_QUEUE_PATH=/var/hermes/alerts.jsonl
      - ALERT_OFFSET_PATH=/var/hermes/alerts.jsonl.offset
      - PROMETHEUS_URL=http://prometheus:9090
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
    networks:
      - hermes-monitoring
      - ethd_default
    depends_on:
      docker-socket-proxy:
        condition: service_started
    restart: unless-stopped

  hermes-agent:
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - PROMETHEUS_URL=http://prometheus:9090
      - LOKI_URL=http://loki:3100
      - BEACON_URL=http://consensus:5052
      - ALERT_QUEUE_PATH=/var/hermes/alerts.jsonl
      - ALERT_OFFSET_PATH=/var/hermes/alerts.jsonl.offset
      - DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL}
      - NTFY_TOPIC=${NTFY_TOPIC}
    volumes:
      - ./runbooks:/runbooks:ro
      - hermes-data:/var/hermes
    networks:
      - hermes-monitoring
      - ethd_default
    depends_on:
      docker-socket-proxy:
        condition: service_started
      webhook-receiver:
        condition: service_started
    restart: unless-stopped

volumes:
  hermes-data:

networks:
  hermes-monitoring:
  ethd_default:
    external: true
```

**Note on eth-docker network name:** eth-docker's default Docker Compose project name is `ethd`, making the default network `ethd_default`. Verify on your host:

```bash
docker network ls | grep ethd
```

**Note on `depends_on`:** `condition: service_started` controls startup ordering only, not runtime availability. The webhook receiver and hermes-agent are designed to tolerate their dependencies being temporarily unavailable.

---

## 4. Technology Choices

| Function | Choice |
|---|---|
| Metrics | Prometheus |
| Alerting | Alertmanager |
| Logs | Loki |
| Visualization | Grafana |
| Agent | Hermes Agent (nousresearch/hermes-agent) |
| Runtime | Docker Compose |
| Notifications (Tier 1) | Discord (webhook → Bot in Phase 4) |
| Notifications (Tier 2) | ntfy.sh (critical + slashing only) |
| Alert queue | append-only JSONL file (webhook-receiver writes) |
| Alert queue offset | plain-text offset file (`alerts.jsonl.offset`) |
| State / Memory | SQLite WAL (hermes-agent sole writer) |
| Runbooks | YAML |
| Incident Archive | Local filesystem |

---

## 5. Phased Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + jsonl queue | Design complete |
| 2 | Hermes integration + runbook matching + operator notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine + socket-proxy migration + Discord Bot API migration | Design complete |
| 5 | Runbook synthesis from historical incidents | Design pending |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

Phases 1-3 are read-only: Hermes receives, analyzes, and notifies. No autonomous actions. Phase 4 adds approval-gated runbook execution and requires the socket-proxy migration and the Discord Bot API migration. Phase 5 requires sufficient incident history. Phase 6 is exploratory.

---

## 6. Key Design Constraints

- Hermes is the **reasoning layer**, not the monitoring system
- Detection stays deterministic (Prometheus rules, Loki ruler)
- LLM never touches raw telemetry streams
- Slashing risk is **always** priority 0, never deferred, never autonomously remediated
- All approvals have explicit timeouts — silence means no action
- Every incident, outcome, and correction is persisted
- The system is safe and useful on day 1 with no autonomous action enabled
- Docker socket access uses the **socket-proxy pattern**
- The **webhook receiver is the sole writer to alerts.jsonl**; hermes-agent is the sole writer to SQLite
- The webhook receiver runs as a **separate process** from the main Hermes agent
- Telemetry plane health (Prometheus, Loki, Alertmanager) is monitored at the same level as validator alerts
- `SlashingProtectionDBError` fires on first Loki ruler evaluation — no `for` clause, no confirmation window
- Discord mentions use `@here` by default — `@everyone` is opt-in via `DISCORD_SLASHING_MENTION=everyone`
- The eth-docker external network is `ethd_default` by default — verify with `docker network ls` before deploying
- Alertmanager POSTs to `http://webhook-receiver:8090/webhook` via Docker DNS
- The alert queue offset is persisted at `/var/hermes/alerts.jsonl.offset` and written atomically via tempfile + rename
- Phase 4 requires replacing `DOCKER_HOST=tcp://docker-socket-proxy:2375` with `tcp://hermes-docker-proxy:2376`

---

## Related Documents

- [Webhook Receiver Spec](webhook-receiver-spec.md) — queue design, dedup, context snapshot, storm protection
- [Slashing Protocol](slashing-protocol.md) — slashing detection, response protocol, evidence preservation
- [Runbook Spec](runbook-spec.md) — runbook schema, approval model, fatigue prevention
- [Memory and Feedback](memory-and-feedback.md) — DB schema, feedback loop, host baselines
- [Alert Set](alert-set.md) — initial alerts, telemetry health alerts
- [Notification Design](notification-design.md) — two-tier routing, Discord, ntfy, env vars
