"""Approval state machine and fatigue prevention for action proposals."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .database import Database

log = logging.getLogger(__name__)

# Cooldowns: don't re-propose the same action for the same incident
# within this window after a prior proposal of any outcome.
REPROPOSAL_COOLDOWN: dict[str, timedelta] = {
    "low": timedelta(hours=4),
    "medium": timedelta(hours=2),
    "high": timedelta(hours=1),
    "critical": timedelta(minutes=30),
}


@dataclass
class ProposalGroup:
    """A group of pending proposals for bundled approval."""

    proposals: list[dict]
    grouped: bool = False


def should_propose_action(action_id: str, incident_id: str, db: Database) -> bool:
    """
    Check if an action should be proposed based on fatigue prevention rules.

    Returns False if:
    - The action was previously skipped or suppressed for this incident.
    - The cooldown period has not elapsed since the last proposal.
    """
    last = db.get_last_proposal(action_id=action_id, incident_id=incident_id)
    if not last:
        return True

    # Never re-propose a SKIPPED or SUPPRESSED action in the same incident
    if last.get("outcome") in ("skipped", "suppressed"):
        log.debug(
            "Action %s %s for incident %s, will not re-propose",
            last.get("outcome"),
            action_id,
            incident_id,
        )
        return False

    # Don't re-propose within cooldown
    elapsed = datetime.now() - datetime.fromisoformat(last["proposed_at"])
    cooldown = REPROPOSAL_COOLDOWN.get(last["severity"], timedelta(hours=1))

    if elapsed < cooldown:
        log.debug(
            "Action %s for incident %s is within cooldown (%s < %s)",
            action_id,
            incident_id,
            elapsed,
            cooldown,
        )
        return False

    return True


def check_timeout_escalation(
    action_id: str,
    incident_id: str,
    db: Database,
    notify_callback: Optional[Callable[..., None]] = None,
) -> None:
    """
    Check if an action has timed out too many times and escalate.

    If the operator has not responded to 2 consecutive proposals for the same
    action in the same incident, stop proposing and escalate.
    """
    timeouts = db.count_timeouts(action_id=action_id, incident_id=incident_id)
    if timeouts >= 2:
        log.warning(
            "Action %s has timed out %d times for incident %s. Escalating.",
            action_id,
            timeouts,
            incident_id,
        )
        db.mark_action_suppressed(action_id=action_id, incident_id=incident_id)

        if notify_callback:
            notify_callback(
                f"Action '{action_id}' has timed out {timeouts} times with no operator response. "
                f"No further proposals will be sent for this incident. Manual intervention required."
            )


def group_pending_proposals(
    host: str, db: Database, within_minutes: int = 5
) -> list[ProposalGroup]:
    """
    Group pending proposals for a host within a time window.

    If multiple suggested_actions from different runbooks are ready to be
    proposed within the window for the same host, bundle them into a single
    approval message rather than sending individually.
    """
    within_seconds = within_minutes * 60
    pending = db.get_pending_proposals(host=host, within_seconds=within_seconds)

    if len(pending) <= 1:
        return [ProposalGroup(proposals=pending)]

    return [ProposalGroup(proposals=pending, grouped=True)]


def propose_action(
    action_id: str,
    incident_id: str,
    severity: str,
    db: Database,
) -> Optional[str]:
    """
    Propose an action if fatigue rules allow it.

    Returns the new proposal ID if proposed, or None if suppressed by fatigue rules.
    """
    if not should_propose_action(action_id, incident_id, db):
        return None

    proposed_at = datetime.now().isoformat()
    proposal_id = db.insert_action_proposal(
        incident_id=incident_id,
        action_id=action_id,
        severity=severity,
        proposed_at=proposed_at,
    )
    log.info(
        "Proposed action %s for incident %s (ID: %s)",
        action_id,
        incident_id,
        proposal_id,
    )
    return proposal_id


def resolve_proposal(
    proposal_id: str,
    outcome: str,
    db: Database,
    resolved_at: Optional[str] = None,
) -> None:
    """
    Resolve a proposal with a final outcome.

    Valid outcomes: 'approved', 'skipped', 'timeout', 'suppressed', 'success', 'failed'
    """
    valid_outcomes = {
        "approved",
        "skipped",
        "timeout",
        "suppressed",
        "success",
        "failed",
    }
    if outcome not in valid_outcomes:
        raise ValueError(
            f"Invalid outcome '{outcome}'. Must be one of {valid_outcomes}"
        )

    if resolved_at is None:
        resolved_at = datetime.now().isoformat()

    db.update_proposal_outcome(
        proposal_id=proposal_id, outcome=outcome, resolved_at=resolved_at
    )
    log.info("Resolved proposal %s with outcome: %s", proposal_id, outcome)
