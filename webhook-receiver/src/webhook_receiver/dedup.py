"""Deduplication logic for incoming alerts.

Reads the incidents table from the SQLite database (read-only)
to determine whether a new alert should be processed or suppressed.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .types import HermesAlert, Severity, SEVERITY_ORDER

log = logging.getLogger(__name__)

# Cooldown windows by severity
COOLDOWN = {
    Severity.CRITICAL: timedelta(minutes=15),
    Severity.HIGH: timedelta(hours=1),
    Severity.MEDIUM: timedelta(hours=4),
}


class DedupLookup:
    """Read-only SQLite accessor for deduplication lookups."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = Path(db_path) if db_path else None

    def get_last_processed(self, alert_type: str, host: str) -> Optional[dict]:
        """
        Return the most recent incident for (alert_type, host) from SQLite.

        Returns None if the DB doesn't exist, is unreadable, or no matching
        record exists — in which case the alert should always be processed.

        Returns dict with keys: severity, status, processed_at (datetime).
        """
        if not self.db_path or not self.db_path.exists():
            return None

        try:
            # Open in read-only mode with WAL for concurrent access
            uri = f"file:{self.db_path}?mode=ro"
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT severity, resolved_at, fired_at
                    FROM incidents
                    WHERE alert_type = ? AND host = ?
                    ORDER BY fired_at DESC
                    LIMIT 1
                    """,
                    (alert_type, host),
                )
                row = cursor.fetchone()

                if row is None:
                    return None

                return {
                    "severity": row["severity"],
                    "status": "resolved" if row["resolved_at"] else "firing",
                    "processed_at": self._parse_dt(row["fired_at"]),
                }
        except (sqlite3.Error, OSError) as e:
            log.warning("Dedup lookup failed: %s — processing alert", e)
            return None  # Fail open: process the alert

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        # Handle ISO-8601 strings
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None


def should_process(alert: HermesAlert, lookup: DedupLookup) -> bool:
    """
    Determine whether an alert should be processed or deduplicated.

    Rules:
    1. No prior incident for (type, host) → always process
    2. Prior incident resolved, new one firing → always process
    3. New severity is higher (more urgent) than prior → always process
    4. Time since prior incident > cooldown → process
    5. Otherwise → deduplicate (skip)
    """
    last = lookup.get_last_processed(alert.alert_type, alert.host)
    if last is None:
        return True

    # Rule 2: resolved → firing is always a new incident
    if last["status"] == "resolved" and alert.status.value == "firing":
        return True

    # Rule 3: higher severity always breaks through dedup
    current_order = SEVERITY_ORDER.get(alert.severity, 99)
    last_order = SEVERITY_ORDER.get(Severity(last["severity"]), 99)
    if current_order < last_order:
        return True

    # Rule 4: cooldown check
    if last["processed_at"] is None:
        return True  # No timestamp, can't determine cooldown

    cooldown = COOLDOWN.get(alert.severity, timedelta(hours=1))
    elapsed = _now_utc() - last["processed_at"]
    if elapsed > cooldown:
        return True

    # Rule 5: deduplicate
    log.info(
        "Deduplicating  id=%s  type=%s  host=%s  last_processed=%s ago  cooldown=%s",
        alert.id,
        alert.alert_type,
        alert.host,
        elapsed,
        cooldown,
    )
    return False


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
