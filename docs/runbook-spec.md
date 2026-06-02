# Runbook Specification

## Three-Tier Action Classification

```yaml
id: consensus_desync

triggers:
  - alert_type: consensus_desync
  - min_severity: high

diagnostics:                          # TIER 1: always run, no approval, no notification
  - id: fetch_sync_status
    cmd: "curl -s http://consensus:5052/eth/v1/node/syncing"
    timeout: 5s
  - id: fetch_peer_count
    cmd: "curl -s http://consensus:5052/eth/v1/node/peer_count"
  - id: tail_logs
    cmd: "docker logs consensus --tail 100 --since 10m"

suggested_actions:                    # TIER 2: Hermes proposes, operator approves each
  - id: restart_consensus_client
    description: "Restart the consensus client container"
    cmd: "docker restart consensus"
    risk: low
    reversible: true
    requires_approval: true
    approval_timeout: 30m
    pre_conditions:
      - "peer_count < 5"
      - "container_status == running"

privileged_actions:                   # TIER 3: locked in Phases 1-3, require explicit unlock
  - id: restore_from_checkpoint
    description: "Wipe state and re-sync from checkpoint"
    risk: high
    reversible: false
    requires_approval: true
    requires_explicit_unlock: true
    phase: 4_and_above_only
```

## Approval State Machine

```
PROPOSED → APPROVED → EXECUTING → SUCCESS
         ↘ SKIPPED               ↘ FAILED
         ↘ TIMEOUT
```

Every state transition is written to the incident record for full audit trail.

## Approval Fatigue Prevention

Approval fatigue occurs when an operator sees repeated identical proposals and starts approving without reading. The following rules prevent this:

```python
# Cooldowns: don't re-propose the same action for the same incident
# within this window after a prior proposal of any outcome.
REPROPOSAL_COOLDOWN: dict[str, timedelta] = {
    "low":      timedelta(hours=4),
    "medium":   timedelta(hours=2),
    "high":     timedelta(hours=1),
    "critical": timedelta(minutes=30),
}

def should_propose_action(action_id: str, incident_id: str, db: DB) -> bool:
    last = db.get_last_proposal(action_id=action_id, incident_id=incident_id)
    if not last:
        return True
    # Never re-propose a SKIPPED action in the same incident — operator said no
    if last.outcome == "skipped":
        return False
    # Don't re-propose within cooldown
    elapsed = now() - last.proposed_at
    cooldown = REPROPOSAL_COOLDOWN[last.severity]
    return elapsed >= cooldown


# Escalation: if the operator has not responded to 2 consecutive proposals
# for the same action in the same incident, stop proposing and escalate.
def check_timeout_escalation(action_id: str, incident_id: str, db: DB) -> None:
    timeouts = db.count_timeouts(action_id=action_id, incident_id=incident_id)
    if timeouts >= 2:
        notify_escalation(
            message=(
                f"Action '{action_id}' has timed out twice with no operator response. "
                f"No further proposals will be sent for this incident. "
                f"Manual intervention required."
            )
        )
        db.mark_action_suppressed(action_id=action_id, incident_id=incident_id)


# Grouping: if multiple suggested_actions from different runbooks are ready
# to be proposed within a 5-minute window for the same host, bundle them
# into a single approval message rather than sending individually.
def group_pending_proposals(host: str, db: DB) -> list[ProposalGroup]:
    pending = db.get_pending_proposals(host=host, within=timedelta(minutes=5))
    if len(pending) <= 1:
        return [ProposalGroup(proposals=pending)]
    return [ProposalGroup(proposals=pending, grouped=True)]
```

## Approval Message Cadence Rules

| Condition | Behaviour |
|---|---|
| First proposal for an action | Send immediately |
| Operator skipped | Never re-propose in same incident |
| Operator approved | Re-propose only if action failed and cooldown elapsed |
| Timeout (no response) | Re-propose once after cooldown; suppress after 2nd timeout |
| Multiple actions within 5 min | Bundle into single grouped approval message |
| Incident resolved externally | Withdraw all pending proposals, send one cancellation notice |

---

## Related Documents

- [Architecture](architecture.md) — system design, phased roadmap
- [Memory and Feedback](memory-and-feedback.md) — action_proposals table, DB queries
- [Slashing Protocol](slashing-protocol.md) — slashing runbook treatment
