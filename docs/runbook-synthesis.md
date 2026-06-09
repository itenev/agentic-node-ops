# Runbook Synthesis Design

## Objective
Automatically generate or refine YAML runbooks based on historical incident data, operator corrections, and successful runbook outcomes. This transforms reactive incident response into proactive, self-improving automation.

## Core Principle
**Synthesis is advisory, never autonomous.** Generated runbooks are drafted to a `runbooks/drafts/` directory and require explicit human review and promotion before they can be executed by the agent.

## Minimum Data Requirements
Synthesis is only triggered when sufficient statistical confidence exists. The minimum thresholds are:
- **Incident Volume**: At least 10 resolved incidents of the same `alert_type` + `host` (or `alert_type` globally for host-agnostic issues).
- **Resolution Consistency**: >= 70% of those incidents must have `outcome = "resolved"`.
- **Action Consistency**: A specific `action_id` or sequence of actions must appear in >= 60% of the successful resolutions.

If these thresholds are not met, the synthesis job logs a skip message and exits cleanly.

## Synthesis Algorithm
1. **Query Candidate Patterns**: 
   ```sql
   SELECT alert_type, host, COUNT(*) as incident_count
   FROM incidents 
   WHERE outcome = 'resolved' 
   GROUP BY alert_type, host 
   HAVING incident_count >= 10;
   ```
2. **Extract Successful Actions**: For each candidate pattern, query `runbook_outcomes` and `incidents.actions_taken` to find the most frequently successful actions.
3. **Incorporate Operator Corrections**: Query `operator_corrections` for the same `alert_type` + `host`. If operators consistently note a specific diagnostic step (e.g., "always check peer count first"), elevate it to a Tier 1 diagnostic in the draft.
4. **Generate Draft YAML**: Construct a `Runbook` object matching the schema in `runbook-spec.md`, populating `trigger`, `diagnostics`, and `actions` based on the extracted patterns.
5. **Write to Drafts**: Save the generated YAML to `runbooks/drafts/{alert_type}_{host}_draft.yaml` with a header comment indicating the data source and confidence score.

## Output Format
Draft runbooks include metadata for human reviewers:
```yaml
# AUTO-GENERATED DRAFT
# Generated: 2025-01-15T10:00:00Z
# Confidence Score: 85% (based on 12 resolved incidents)
# Top successful action: restart_consensus (8/12 times)
# Operator corrections applied: 2

id: consensus_desync_auto
description: Auto-generated runbook for consensus desync based on historical success patterns.
trigger:
  alert_type: consensus_desync
diagnostics:
  - id: check_peers
    cmd: "curl -s http://consensus:5052/eth/v1/node/peer_count"
    description: "Check peer count (frequently cited in operator corrections)"
actions:
  - id: restart_consensus
    description: "Restart consensus client (successful in 67% of resolved incidents)"
    cmd: "docker restart consensus"
    risk: medium
    reversible: true
    requires_approval: true
```

## Human Review Gate
1. A daily cron job (or systemd timer) runs the synthesis script.
2. If new drafts are generated, Hermes sends a Tier 1 Discord notification to the operator: *"New runbook draft generated: `consensus_desync_auto.yaml`. Review required."*
3. The operator reviews the draft, edits if necessary, and moves it to the root `runbooks/` directory to activate it.
4. Once promoted, the synthesis job tracks its performance via the standard `runbook_outcomes` table, creating a closed feedback loop.

## Implementation Files
- Create: `src/agentic_node_ops/synthesis.py` (orchestrator and YAML generator)
- Create: `tests/test_synthesis.py`
- Create: `runbooks/drafts/.gitkeep`

---

## Related Documents
- [Runbook Spec](runbook-spec.md) — target schema for generated drafts
- [Memory and Feedback](memory-and-feedback.md) — source tables (`incidents`, `operator_corrections`, `runbook_outcomes`)
