# Functional Testing Runbook

> **Objective:** Validate the end-to-end agentic node ops pipeline on a real `eth-docker` deployment before accumulating production data for Phase 5.
> **Prerequisite:** An active `eth-docker` deployment with Prometheus, Alertmanager, and Loki running.

---

## 1. Pre-Flight Checks

### 1.1 Verify Environment Variables
Ensure the following are set in your `.env` file (or passed via environment) before deployment:
```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
NTFY_TOPIC="https://ntfy.sh/your-secret-topic"
```

### 1.2 Verify Alertmanager Routing
Add the following route to your `eth-docker` Alertmanager configuration (`alertmanager/config.yml`) to ensure alerts are routed to the webhook receiver. (See `docs/alertmanager-hermes-routing.yml` for the full configuration).

```yaml
route:
  receiver: 'hermes-webhook'
  group_by: ['alertname', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: 'hermes-webhook'
    webhook_configs:
      - url: 'http://webhook-receiver:8090/webhook'
        send_resolved: true
```
*Note: Adjust the URL if deploying outside the same Docker network, or ensure `webhook-receiver` is resolvable.*

### 1.3 Verify Prometheus Rules
Ensure the self-monitoring rules are loaded. If using `eth-docker`, place `monitoring/rules/hermes.yml` in the Prometheus rules directory and reload Prometheus.

---

## 2. Deployment

### 2.1 Build and Start the Stack
From the `agentic-node-ops` root directory:
```bash
# Ensure the eth-docker network exists
docker network ls | grep ethd_default

# Build and start the Hermes stack
docker compose up -d --build
```

### 2.2 Verify Health Endpoints
```bash
# Check webhook receiver health
curl http://localhost:8090/health

# Check hermes-agent metrics (should return Prometheus text format)
curl http://localhost:8091/metrics | grep hermes_alive
```
*Expected:* `hermes_alive 1.0`

---

## 3. Test Scenarios

### Scenario A: Basic Alert Processing & Notification (Tier 1)
**Goal:** Verify alert ingestion, context assembly, and two-tier notification.

1. **Trigger:** Manually fire a test alert directly to the webhook receiver (isolates the pipeline from Alertmanager configuration):
   ```bash
   curl -X POST http://localhost:8090/webhook \
     -H "Content-Type: application/json" \
     -d '{
       "receiver": "hermes-webhook",
       "status": "firing",
       "alerts": [{
         "status": "firing",
         "labels": {
           "alertname": "ConsensusDesync",
           "severity": "high",
           "instance": "test-host"
         },
         "annotations": {"summary": "Test alert"},
         "startsAt": "2025-01-01T00:00:00Z",
         "fingerprint": "test-001"
       }]
     }'
   ```
2. **Validate Webhook Receiver:**
   ```bash
   docker compose logs webhook-receiver | grep "Processed alert"
   ```
   *Expected:* Log showing the alert was written to `alerts.jsonl`.
3. **Validate Hermes Agent:**
   ```bash
   docker compose logs hermes-agent | grep "Processed alert id="
   ```
   *Expected:* Log showing the alert was read, context was built, and dispatched.
4. **Validate Notifications:** Check Discord and ntfy.sh for the incoming alert. The message should include the `hermes_context` summary.

### Scenario B: Storm Protection
**Goal:** Verify that rapid, repeated alerts are bundled.

1. **Trigger:** Fire 4+ identical alerts for the same `host` within 30 seconds.
2. **Validate:** Check `webhook-receiver` logs:
   ```bash
   docker compose logs webhook-receiver | grep "storm_single_host"
   ```
   *Expected:* A single bundled alert is written to `alerts.jsonl` with `alert_type: storm_single_host`.

### Scenario C: Deduplication
**Goal:** Verify that resolved alerts do not spam, but re-firing alerts after resolution do process.

1. **Trigger:** Fire an alert, let it process, then resolve it (send `status: resolved`).
2. **Trigger Again:** Fire the exact same alert immediately.
3. **Validate:** Check `webhook-receiver` logs for `Deduplicated` or `skipped`.
   *Expected:* The second firing is skipped. If you wait past the cooldown (e.g., 1 hour for high severity) or change severity to `critical`, it should process again.

### Scenario D: Approval Flow & Execution (Tier 2/3)
**Goal:** Verify the state machine, fatigue prevention, and safe execution.

1. **Trigger:** Fire an alert that matches a runbook with `requires_approval: true` (e.g., `client_crash`).
2. **Validate Proposal:** Check `hermes-agent` logs for `Proposing action`.
3. **Validate Database:** Inspect the SQLite DB for the pending proposal (note: `outcome` is `NULL` when pending):
   ```bash
   docker compose exec hermes-agent sqlite3 /var/hermes/incidents.db \
     "SELECT ap.id, i.alert_type, ap.action_id, ap.outcome FROM action_proposals ap JOIN incidents i ON ap.incident_id = i.id WHERE ap.outcome IS NULL;"
   ```
4. **Simulate Approval:** (In a real scenario, this is done via UI/API. For testing, update the DB directly):
   ```bash
   docker compose exec hermes-agent sqlite3 /var/hermes/incidents.db \
     "UPDATE action_proposals SET outcome = 'approved', resolved_at = datetime('now') WHERE outcome IS NULL;"
   ```
5. **Validate Execution:** Check `hermes-agent` logs for `Executing action` and verify the `executor.py` successfully ran the command (e.g., `docker restart execution`).
6. **Validate Outcome:** Verify a record was written to the `runbook_outcomes` table.

### Scenario E: Self-Monitoring Failure
**Goal:** Verify the agent detects its own silence and recovers from backlog.

1. **Trigger:** Stop the hermes-agent container:
   ```bash
   docker compose stop hermes-agent
   ```
2. **Validate Queue Survival:** Wait 2 minutes. Check Prometheus for the `HermesAgentSilent` alert firing. Alertmanager will route this to the webhook receiver, which will append it to `alerts.jsonl`.
3. **Validate Backlog Drain:** Restart the agent:
   ```bash
   docker compose start hermes-agent
   ```
   *Expected:* Upon restart, `hermes-agent` reads the backlogged `HermesAgentSilent` alert from the queue and sends the critical notification. This validates that the queue survived the outage and the backlog is drained correctly.
   
   > **Design Limitation Note:** Self-monitoring alerts cannot notify you in real-time if `hermes-agent` itself is down, because the agent is the component that dispatches notifications. If real-time silence alerting is required, you must configure a secondary notification path (e.g., Alertmanager routing `HermesAgentSilent` directly to a Discord webhook in addition to the Hermes receiver).

---

## 4. Post-Test Cleanup & Data Verification

### 4.1 Verify Phase 5 Data Accumulation
Ensure the database is correctly logging outcomes for future synthesis:
```bash
docker compose exec hermes-agent sqlite3 /var/hermes/incidents.db \
  "SELECT COUNT(*) FROM incidents; SELECT COUNT(*) FROM runbook_outcomes;"
```
*Expected:* Counts > 0 if Scenario A and D were successful.

### 4.2 Teardown (Optional)
```bash
docker compose down -v  # Removes volumes (WARNING: deletes accumulated test data)
```

---

## 5. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `webhook-receiver` returns 500 | Prometheus/Loki unreachable | Verify `PROMETHEUS_URL` and network connectivity (`docker compose exec webhook-receiver curl http://prometheus:9090`) |
| `hermes-agent` fails to execute action | Nginx proxy blocking POST | Check `hermes-docker-proxy` logs. Ensure `nginx-docker-filter.conf` allows `POST /containers/{name}/restart` |
| Notifications not arriving | Missing env vars | Verify `DISCORD_WEBHOOK_URL` and `NTFY_TOPIC` are set in `.env` |
| `hermes_alive` is 0 | Agent crashed or deadlocked | Check `docker compose logs hermes-agent` for tracebacks |
