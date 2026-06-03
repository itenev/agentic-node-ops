"""Tests for the deduplication logic."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webhook_receiver.dedup import DedupLookup, should_process, COOLDOWN
from webhook_receiver.types import HermesAlert, Severity, AlertStatus, _generate_event_id


def _make_alert(alert_type="consensus_desync", severity=Severity.HIGH, host="validator-01") -> HermesAlert:
    return HermesAlert(
        id=_generate_event_id(alert_type),
        alert_type=alert_type,
        severity=severity,
        status=AlertStatus.FIRING,
        host=host,
    )


@pytest.fixture
def lookup_with_db(tmp_path):
    """Create a DedupLookup backed by a temporary SQLite DB."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE incidents (
            id TEXT PRIMARY KEY,
            alert_type TEXT NOT NULL,
            host TEXT NOT NULL,
            severity TEXT NOT NULL,
            fired_at DATETIME,
            resolved_at DATETIME
        )
    """)
    conn.commit()
    conn.close()
    return DedupLookup(db_path=str(db_path))


@pytest.fixture
def lookup_no_db(tmp_path):
    """Create a DedupLookup pointing to a non-existent DB."""
    return DedupLookup(db_path=str(tmp_path / "nonexistent.db"))


class TestDedupLookup:
    def test_no_prior_incident_returns_none(self, lookup_with_db):
        result = lookup_with_db.get_last_processed("consensus_desync", "validator-01")
        assert result is None

    def test_returns_most_recent_incident(self, lookup_with_db):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", (now - timedelta(hours=2)).isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t2", "consensus_desync", "validator-01", "medium", now.isoformat(), None),
        )
        conn.commit()
        conn.close()

        result = lookup_with_db.get_last_processed("consensus_desync", "validator-01")
        assert result is not None
        assert result["severity"] == "medium"  # most recent

    def test_nonexistent_db_returns_none(self, lookup_no_db):
        result = lookup_no_db.get_last_processed("consensus_desync", "validator-01")
        assert result is None

    def test_missing_db_path_returns_none(self):
        lookup = DedupLookup(db_path=None)
        result = lookup.get_last_processed("consensus_desync", "validator-01")
        assert result is None


class TestShouldProcess:
    def test_no_prior_incident_always_processes(self, lookup_no_db):
        alert = _make_alert()
        assert should_process(alert, lookup_no_db) is True

    def test_resolved_to_firing_always_processes(self, lookup_with_db, tmp_path):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", (now - timedelta(minutes=5)).isoformat(), now.isoformat()),
        )
        conn.commit()
        conn.close()

        alert = _make_alert()
        assert should_process(alert, lookup_with_db) is True

    def test_higher_severity_breaks_dedup(self, lookup_with_db):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "medium", now.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(severity=Severity.HIGH)
        assert should_process(alert, lookup_with_db) is True

    def test_same_severity_within_cooldown_is_deduped(self, lookup_with_db):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", now.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(severity=Severity.HIGH)
        assert should_process(alert, lookup_with_db) is False

    def test_same_severity_after_cooldown_processes(self, lookup_with_db):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", past.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(severity=Severity.HIGH)
        assert should_process(alert, lookup_with_db) is True

    def test_different_host_not_deduped(self, lookup_with_db):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", now.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(host="validator-02")
        assert should_process(alert, lookup_with_db) is True

    def test_different_alert_type_not_deduped(self, lookup_with_db):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "high", now.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(alert_type="validator_duty_miss")
        assert should_process(alert, lookup_with_db) is True

    def test_critical_short_cooldown(self, lookup_with_db):
        """Critical cooldown is 15min — alert after 16min should pass."""
        past = datetime.now(timezone.utc) - timedelta(minutes=16)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "critical", past.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(severity=Severity.CRITICAL)
        assert should_process(alert, lookup_with_db) is True

    def test_critical_within_cooldown_deduped(self, lookup_with_db):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        conn = sqlite3.connect(lookup_with_db.db_path)
        conn.execute(
            "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "consensus_desync", "validator-01", "critical", past.isoformat(), None),
        )
        conn.commit()
        conn.close()

        alert = _make_alert(severity=Severity.CRITICAL)
        assert should_process(alert, lookup_with_db) is False
