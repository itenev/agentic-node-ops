"""SQLite database management for agentic-node-ops.

Hermes agent is the SOLE writer to this database.
Uses WAL mode for concurrent read access by the webhook receiver.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                   TEXT PRIMARY KEY,
    alert_type           TEXT NOT NULL,
    host                 TEXT NOT NULL,
    severity             TEXT NOT NULL,
    fired_at             DATETIME,
    resolved_at          DATETIME,
    context_snapshot     JSON,
    hermes_analysis      TEXT,
    runbook_used         TEXT,
    actions_proposed     JSON,
    actions_taken        JSON,
    outcome              TEXT,
    operator_feedback    TEXT,
    feedback_rating      INTEGER,
    duration_to_resolve  INTEGER,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS host_fingerprints (
    host            TEXT NOT NULL,
    metric          TEXT NOT NULL,
    baseline_p50    REAL,
    baseline_p95    REAL,
    last_updated    DATETIME,
    PRIMARY KEY (host, metric)
);

CREATE TABLE IF NOT EXISTS operator_corrections (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    host        TEXT NOT NULL,
    correction  TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runbook_outcomes (
    id              TEXT PRIMARY KEY,
    runbook_id      TEXT NOT NULL,
    host            TEXT NOT NULL,
    action_taken    TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    time_to_resolve INTEGER,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS action_proposals (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    proposed_at DATETIME NOT NULL,
    outcome     TEXT,
    resolved_at DATETIME,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_incidents_alert_host ON incidents(alert_type, host);
CREATE INDEX IF NOT EXISTS idx_corrections_alert_host ON operator_corrections(alert_type, host);
"""


class Database:
    """SQLite database wrapper with WAL mode enabled."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = Path(
            db_path or os.environ.get("INCIDENTS_DB_PATH", "/var/hermes/incidents.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database and enable WAL mode."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA)
            log.info("Database initialized at %s (WAL mode)", self.db_path)

    @contextmanager
    def _get_connection(self):
        """Yield a database connection, ensuring it is closed."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def insert_incident(self, alert: dict) -> None:
        """Insert a new incident record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO incidents 
                (id, alert_type, host, severity, fired_at, context_snapshot)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    alert["id"],
                    alert["alert_type"],
                    alert["host"],
                    alert["severity"],
                    alert.get("fired_at"),
                    json.dumps(alert.get("context_snapshot", {})),
                ),
            )
            conn.commit()

    def update_incident(
        self,
        incident_id: str,
        resolved_at: Optional[str] = None,
        hermes_analysis: Optional[str] = None,
        runbook_used: Optional[str] = None,
        actions_proposed: Optional[list] = None,
        actions_taken: Optional[list] = None,
        outcome: Optional[str] = None,
        operator_feedback: Optional[str] = None,
        feedback_rating: Optional[int] = None,
        duration_to_resolve: Optional[int] = None,
    ) -> None:
        """Update an existing incident record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE incidents SET
                    resolved_at = COALESCE(?, resolved_at),
                    hermes_analysis = COALESCE(?, hermes_analysis),
                    runbook_used = COALESCE(?, runbook_used),
                    actions_proposed = COALESCE(?, actions_proposed),
                    actions_taken = COALESCE(?, actions_taken),
                    outcome = COALESCE(?, outcome),
                    operator_feedback = COALESCE(?, operator_feedback),
                    feedback_rating = COALESCE(?, feedback_rating),
                    duration_to_resolve = COALESCE(?, duration_to_resolve)
                WHERE id = ?
                """,
                (
                    resolved_at,
                    hermes_analysis,
                    runbook_used,
                    json.dumps(actions_proposed) if actions_proposed else None,
                    json.dumps(actions_taken) if actions_taken else None,
                    outcome,
                    operator_feedback,
                    feedback_rating,
                    duration_to_resolve,
                    incident_id,
                ),
            )
            conn.commit()

    def get_recent_incidents(
        self, alert_type: str, host: str, limit: int = 5
    ) -> list[dict]:
        """Get recent incidents for a specific alert type and host."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, alert_type, host, severity, fired_at, outcome, hermes_analysis
                FROM incidents
                WHERE alert_type = ? AND host = ?
                ORDER BY fired_at DESC
                LIMIT ?
                """,
                (alert_type, host, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_corrections(self, alert_type: str, host: str) -> list[str]:
        """Get operator corrections for a specific alert type and host."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT correction
                FROM operator_corrections
                WHERE alert_type = ? AND host = ?
                ORDER BY created_at DESC
                """,
                (alert_type, host),
            )
            return [row["correction"] for row in cursor.fetchall()]

    def insert_correction(
        self, incident_id: str, alert_type: str, host: str, correction: str
    ) -> None:
        """Insert an operator correction."""
        import uuid

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO operator_corrections (id, incident_id, alert_type, host, correction)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), incident_id, alert_type, host, correction),
            )
            conn.commit()

    def get_last_processed(self, alert_type: str, host: str) -> Optional[dict]:
        """Get the most recent incident for deduplication lookups (read-only)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, alert_type, host, severity, fired_at, outcome
                FROM incidents
                WHERE alert_type = ? AND host = ?
                ORDER BY fired_at DESC
                LIMIT 1
                """,
                (alert_type, host),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
