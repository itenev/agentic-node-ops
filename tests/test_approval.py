"""Tests for approval state machine and fatigue prevention."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_node_ops.approval import (
    check_timeout_escalation,
    group_pending_proposals,
    propose_action,
    resolve_proposal,
    should_propose_action,
)
from agentic_node_ops.database import Database


@pytest.fixture
def temp_db():
    """Provide a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_approval.db"
        db = Database(db_path=str(db_path))
        yield db


def test_should_propose_action_first_time(temp_db: Database):
    """Test that a new action can be proposed."""
    result = should_propose_action("action_1", "incident_1", temp_db)
    assert result is True


def test_should_propose_action_skipped(temp_db: Database):
    """Test that a skipped action is never re-proposed."""
    temp_db.insert_action_proposal(
        incident_id="incident_1",
        action_id="action_1",
        severity="high",
        proposed_at=(datetime.now() - timedelta(hours=2)).isoformat(),
    )
    # Manually update to skipped for testing
    temp_db.update_proposal_outcome(
        proposal_id=temp_db.get_last_proposal("action_1", "incident_1")["id"],
        outcome="skipped",
    )

    result = should_propose_action("action_1", "incident_1", temp_db)
    assert result is False


def test_should_propose_action_within_cooldown(temp_db: Database):
    """Test that an action within cooldown is not proposed."""
    temp_db.insert_action_proposal(
        incident_id="incident_1",
        action_id="action_1",
        severity="high",  # 1 hour cooldown
        proposed_at=(datetime.now() - timedelta(minutes=30)).isoformat(),
    )

    result = should_propose_action("action_1", "incident_1", temp_db)
    assert result is False


def test_should_propose_action_after_cooldown(temp_db: Database):
    """Test that an action after cooldown can be proposed."""
    temp_db.insert_action_proposal(
        incident_id="incident_1",
        action_id="action_1",
        severity="high",  # 1 hour cooldown
        proposed_at=(datetime.now() - timedelta(hours=2)).isoformat(),
    )

    result = should_propose_action("action_1", "incident_1", temp_db)
    assert result is True


def test_propose_action_creates_record(temp_db: Database):
    """Test that propose_action creates a DB record and returns ID."""
    proposal_id = propose_action("action_1", "incident_1", "high", temp_db)

    assert proposal_id is not None
    last = temp_db.get_last_proposal("action_1", "incident_1")
    assert last["id"] == proposal_id
    assert last["outcome"] is None


def test_propose_action_respects_fatigue(temp_db: Database):
    """Test that propose_action returns None if fatigue rules block it."""
    # First proposal
    propose_action("action_1", "incident_1", "high", temp_db)
    temp_db.update_proposal_outcome(
        proposal_id=temp_db.get_last_proposal("action_1", "incident_1")["id"],
        outcome="skipped",
    )

    # Second attempt should be blocked
    proposal_id = propose_action("action_1", "incident_1", "high", temp_db)
    assert proposal_id is None


def test_resolve_proposal_updates_record(temp_db: Database):
    """Test that resolve_proposal updates the outcome."""
    proposal_id = propose_action("action_1", "incident_1", "high", temp_db)
    resolve_proposal(proposal_id, "approved", temp_db)

    last = temp_db.get_last_proposal("action_1", "incident_1")
    assert last["outcome"] == "approved"
    assert last["resolved_at"] is not None


def test_resolve_proposal_invalid_outcome(temp_db: Database):
    """Test that resolve_proposal raises ValueError for invalid outcome."""
    proposal_id = propose_action("action_1", "incident_1", "high", temp_db)

    with pytest.raises(ValueError, match="Invalid outcome"):
        resolve_proposal(proposal_id, "invalid_outcome", temp_db)


def test_count_timeouts(temp_db: Database):
    """Test that count_timeouts returns correct count."""
    from datetime import datetime

    # Insert 2 timeouts and 1 approved directly to bypass fatigue checks
    for _ in range(2):
        temp_db.insert_action_proposal(
            incident_id="incident_1",
            action_id="action_1",
            severity="high",
            proposed_at=datetime.now().isoformat(),
        )
        last = temp_db.get_last_proposal("action_1", "incident_1")
        temp_db.update_proposal_outcome(last["id"], "timeout")

    temp_db.insert_action_proposal(
        incident_id="incident_1",
        action_id="action_1",
        severity="high",
        proposed_at=datetime.now().isoformat(),
    )
    last = temp_db.get_last_proposal("action_1", "incident_1")
    temp_db.update_proposal_outcome(last["id"], "approved")

    count = temp_db.count_timeouts("action_1", "incident_1")
    assert count == 2


def test_check_timeout_escalation(temp_db: Database):
    """Test that check_timeout_escalation suppresses after 2 timeouts."""
    from datetime import datetime

    # Insert 2 timeouts directly to bypass fatigue checks
    for _ in range(2):
        temp_db.insert_action_proposal(
            incident_id="incident_1",
            action_id="action_1",
            severity="high",
            proposed_at=datetime.now().isoformat(),
        )
        last = temp_db.get_last_proposal("action_1", "incident_1")
        temp_db.update_proposal_outcome(last["id"], "timeout")

    mock_notify = MagicMock()
    check_timeout_escalation(
        "action_1", "incident_1", temp_db, notify_callback=mock_notify
    )

    # Verify suppression (the last proposal should be marked suppressed)
    last = temp_db.get_last_proposal("action_1", "incident_1")
    assert last["outcome"] == "suppressed"

    # Verify notification was called
    mock_notify.assert_called_once()
    assert "timed out 2 times" in mock_notify.call_args[0][0]


def test_group_pending_proposals_single(temp_db: Database):
    """Test grouping with a single pending proposal."""
    # Insert an incident first
    temp_db.insert_incident(
        {
            "id": "incident_1",
            "alert_type": "test_alert",
            "host": "node-1",
            "severity": "high",
            "fired_at": datetime.now().isoformat(),
        }
    )

    propose_action("action_1", "incident_1", "high", temp_db)

    groups = group_pending_proposals("node-1", temp_db)
    assert len(groups) == 1
    assert groups[0].grouped is False
    assert len(groups[0].proposals) == 1


def test_group_pending_proposals_multiple(temp_db: Database):
    """Test grouping with multiple pending proposals."""
    # Insert an incident first
    temp_db.insert_incident(
        {
            "id": "incident_1",
            "alert_type": "test_alert",
            "host": "node-1",
            "severity": "high",
            "fired_at": datetime.now().isoformat(),
        }
    )

    propose_action("action_1", "incident_1", "high", temp_db)
    propose_action("action_2", "incident_1", "high", temp_db)

    groups = group_pending_proposals("node-1", temp_db)
    assert len(groups) == 1
    assert groups[0].grouped is True
    assert len(groups[0].proposals) == 2
