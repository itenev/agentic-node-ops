"""Tests for context assembly and database history queries."""

import tempfile
from pathlib import Path

import pytest

from agentic_node_ops.context import build_hermes_context
from agentic_node_ops.database import Database
from agentic_node_ops.types import NotificationPayload, Severity


@pytest.fixture
def temp_db():
    """Provide a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_incidents.db"
        db = Database(db_path=str(db_path))
        yield db


def test_get_runbook_stats_empty(temp_db: Database):
    """Test get_runbook_stats returns 0.0 success rate when no data exists."""
    stats = temp_db.get_runbook_stats("test_runbook")
    assert stats["success_rate"] == 0.0
    assert stats["failed_cases"] == ["None recorded"]


def test_get_runbook_stats_with_data(temp_db: Database):
    """Test get_runbook_stats calculates success rate correctly."""
    with temp_db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO runbook_outcomes (id, runbook_id, host, action_taken, outcome, time_to_resolve)
            VALUES ('1', 'rb1', 'host1', 'restart', 'resolved', 60),
                   ('2', 'rb1', 'host1', 'restart', 'did_not_help', 30),
                   ('3', 'rb1', 'host2', 'wipe', 'resolved', 120)
            """
        )
        conn.commit()

    stats = temp_db.get_runbook_stats("rb1")
    assert stats["success_rate"] == pytest.approx(2 / 3)
    assert len(stats["failed_cases"]) == 1
    assert "restart (did_not_help)" in stats["failed_cases"][0]


def test_get_host_baselines_empty(temp_db: Database):
    """Test get_host_baselines returns empty dict when no data exists."""
    baselines = temp_db.get_host_baselines("host1")
    assert baselines == {}


def test_get_host_baselines_with_data(temp_db: Database):
    """Test get_host_baselines returns correct metrics."""
    with temp_db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO host_fingerprints (host, metric, baseline_p50, baseline_p95, last_updated)
            VALUES ('host1', 'peer_count', 45.0, 60.0, '2024-01-01'),
                   ('host1', 'validator_count', 10.0, 12.0, '2024-01-01')
            """
        )
        conn.commit()

    baselines = temp_db.get_host_baselines("host1")
    assert len(baselines) == 2
    assert baselines["peer_count"]["p50"] == 45.0
    assert baselines["peer_count"]["p95"] == 60.0
    assert baselines["validator_count"]["p50"] == 10.0


def test_build_hermes_context_no_history(temp_db: Database):
    """Test build_hermes_context formats correctly with no history."""
    payload = NotificationPayload(
        incident_id="inc-1",
        alert_type="consensus_desync",
        severity=Severity.HIGH,
        host="node-1",
        title="Consensus Desync on node-1",
        summary="Node is out of sync",
        diagnostics={"peer_count": "2", "syncing": "true"},
        runbook_id="consensus_desync",
    )

    context = build_hermes_context(payload, temp_db)

    assert "consensus_desync" in context
    assert "node-1" in context
    assert "No prior incidents recorded" in context
    assert "No operator corrections recorded" in context
    assert "0% of the time" in context
    assert "No baselines recorded" in context
    assert '"peer_count": "2"' in context


def test_build_hermes_context_with_history(temp_db: Database):
    """Test build_hermes_context includes history and baselines when present."""
    # Insert incident
    with temp_db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO incidents (id, alert_type, host, severity, fired_at, outcome, hermes_analysis)
            VALUES ('inc-0', 'consensus_desync', 'node-1', 'high', '2024-01-01 10:00:00', 'resolved', 'Restarted peer')
            """
        )
        # Insert correction
        conn.execute(
            """
            INSERT INTO operator_corrections (id, incident_id, alert_type, host, correction)
            VALUES ('corr-1', 'inc-0', 'consensus_desync', 'node-1', 'Always check peer count first')
            """
        )
        # Insert runbook outcome
        conn.execute(
            """
            INSERT INTO runbook_outcomes (id, runbook_id, host, action_taken, outcome, time_to_resolve)
            VALUES ('out-1', 'consensus_desync', 'node-1', 'restart_consensus_client', 'resolved', 60)
            """
        )
        # Insert baseline
        conn.execute(
            """
            INSERT INTO host_fingerprints (host, metric, baseline_p50, baseline_p95, last_updated)
            VALUES ('node-1', 'peer_count', 50.0, 80.0, '2024-01-01')
            """
        )
        conn.commit()

    payload = NotificationPayload(
        incident_id="inc-1",
        alert_type="consensus_desync",
        severity=Severity.HIGH,
        host="node-1",
        title="Consensus Desync on node-1",
        summary="Node is out of sync",
        diagnostics={"peer_count": "2"},
        runbook_id="consensus_desync",
    )

    context = build_hermes_context(payload, temp_db)

    assert "resolved (severity: high, analysis: Restarted peer)" in context
    assert "Always check peer count first" in context
    assert "100% of the time" in context
    assert "peer_count: p50=50.0, p95=80.0" in context
