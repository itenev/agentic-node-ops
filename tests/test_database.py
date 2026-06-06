"""Tests for database module."""

import json
import os
import tempfile
import pytest

from agentic_node_ops.database import Database


@pytest.fixture
def temp_db():
    """Provide a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        yield db_path


def test_database_init(temp_db):
    """Test database initialization creates tables and enables WAL."""
    db = Database(db_path=temp_db)
    with db._get_connection() as conn:
        cursor = conn.execute("PRAGMA journal_mode;")
        assert cursor.fetchone()[0].lower() == "wal"
        
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        assert "incidents" in tables
        assert "host_fingerprints" in tables
        assert "operator_corrections" in tables


def test_insert_and_get_recent_incidents(temp_db):
    """Test inserting and retrieving recent incidents."""
    db = Database(db_path=temp_db)
    
    alert = {
        "id": "test-1",
        "alert_type": "consensus_desync",
        "host": "validator-01",
        "severity": "high",
        "fired_at": "2025-01-01T00:00:00Z",
        "context_snapshot": {"peer_count": 2}
    }
    
    db.insert_incident(alert)
    
    incidents = db.get_recent_incidents("consensus_desync", "validator-01", limit=5)
    assert len(incidents) == 1
    assert incidents[0]["id"] == "test-1"
    assert incidents[0]["severity"] == "high"


def test_update_incident(temp_db):
    """Test updating an incident record."""
    db = Database(db_path=temp_db)
    
    alert = {
        "id": "test-2",
        "alert_type": "peer_disconnect",
        "host": "validator-02",
        "severity": "medium",
        "fired_at": "2025-01-01T00:00:00Z",
    }
    db.insert_incident(alert)
    
    db.update_incident(
        incident_id="test-2",
        hermes_analysis="Network issue detected",
        outcome="resolved",
        duration_to_resolve=120
    )
    
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT hermes_analysis, outcome, duration_to_resolve FROM incidents WHERE id = ?", ("test-2",))
        row = cursor.fetchone()
        assert row["hermes_analysis"] == "Network issue detected"
        assert row["outcome"] == "resolved"
        assert row["duration_to_resolve"] == 120


def test_insert_and_get_corrections(temp_db):
    """Test inserting and retrieving operator corrections."""
    db = Database(db_path=temp_db)
    
    db.insert_correction(
        incident_id="test-3",
        alert_type="sync_lag",
        host="validator-03",
        correction="Check firewall rules"
    )
    
    corrections = db.get_corrections("sync_lag", "validator-03")
    assert len(corrections) == 1
    assert corrections[0] == "Check firewall rules"


def test_get_last_processed(temp_db):
    """Test retrieving the last processed incident for deduplication."""
    db = Database(db_path=temp_db)
    
    alert1 = {
        "id": "test-4a",
        "alert_type": "consensus_desync",
        "host": "validator-01",
        "severity": "high",
        "fired_at": "2025-01-01T00:00:00Z",
    }
    alert2 = {
        "id": "test-4b",
        "alert_type": "consensus_desync",
        "host": "validator-01",
        "severity": "critical",
        "fired_at": "2025-01-01T01:00:00Z",
    }
    
    db.insert_incident(alert1)
    db.insert_incident(alert2)
    
    last = db.get_last_processed("consensus_desync", "validator-01")
    assert last is not None
    assert last["id"] == "test-4b"
    assert last["severity"] == "critical"
