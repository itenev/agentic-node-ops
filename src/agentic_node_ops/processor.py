"""Alert processor: reads jsonl, drains to SQLite, and dispatches.

This is the main loop that processes alerts queued by the webhook receiver.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from prometheus_client import Gauge, start_http_server

from .context import build_hermes_context
from .database import Database
from .dispatcher import NotificationDispatcher
from .types import NotificationPayload, Severity

log = logging.getLogger(__name__)

ALERTS_JSONL_PATH = os.environ.get("ALERTS_JSONL_PATH", "/var/hermes/alerts.jsonl")
ALERT_OFFSET_PATH = os.environ.get(
    "ALERT_OFFSET_PATH", "/var/hermes/alerts.jsonl.offset"
)
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8091"))

# Prometheus metrics
HERMES_ALIVE = Gauge("hermes_alive", "Hermes agent heartbeat (1 = alive, 0 = silent)")


def _start_metrics_server() -> None:
    """Start Prometheus metrics HTTP server in a background thread."""
    try:
        start_http_server(METRICS_PORT)
        log.info("Prometheus metrics server started on port %d", METRICS_PORT)
    except Exception as e:
        log.error("Failed to start metrics server: %s", e)


def read_offset(path: str) -> int:
    """Read current offset (0 if file absent — start from beginning)."""
    try:
        return int(Path(path).read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(path: str, offset: int) -> None:
    """Write new offset atomically via tempfile + rename."""
    tmp_path = path + ".tmp"
    Path(tmp_path).write_text(str(offset))
    os.replace(tmp_path, path)  # atomic on POSIX


def _build_payload(alert: dict) -> NotificationPayload:
    """Convert a raw alert dict into a NotificationPayload."""
    severity_str = alert.get("severity", "medium").lower()
    try:
        severity = Severity(severity_str)
    except ValueError:
        severity = Severity.MEDIUM

    return NotificationPayload(
        incident_id=alert.get("id", "unknown"),
        alert_type=alert.get("alert_type", "unknown"),
        severity=severity,
        host=alert.get("host", "unknown"),
        title=f"{alert.get('alert_type', 'Alert')} on {alert.get('host', 'host')}",
        summary="Alert received. Hermes analysis pending.",
        diagnostics=alert.get("context_snapshot", {}),
        runbook_id=alert.get("runbook_hint"),
    )


async def process_alerts_async(
    db: Optional[Database] = None,
    dispatcher: Optional[NotificationDispatcher] = None,
    limit: int = 100,
) -> int:
    """
    Process up to `limit` alerts from the jsonl queue asynchronously.

    Returns the number of alerts successfully processed.
    """
    db = db or Database()
    dispatcher = dispatcher or NotificationDispatcher()
    jsonl_path = Path(ALERTS_JSONL_PATH)

    if not jsonl_path.exists():
        log.debug("No alerts.jsonl found at %s", jsonl_path)
        return 0

    current_offset = read_offset(ALERT_OFFSET_PATH)
    processed_count = 0

    with open(jsonl_path, "r") as f:
        f.seek(current_offset)

        for _ in range(limit):
            line = f.readline()
            if not line:
                break  # EOF

            line = line.strip()
            if not line:
                continue

            try:
                alert = json.loads(line)
            except json.JSONDecodeError as e:
                log.error("Failed to parse JSON line at offset %d: %s", f.tell(), e)
                # Skip malformed line by advancing offset
                current_offset = f.tell()
                write_offset(ALERT_OFFSET_PATH, current_offset)
                continue

            try:
                # 1. Build payload and enrich with Hermes context (query history *before* insert)
                payload = _build_payload(alert)
                payload.summary = build_hermes_context(payload, db)

                # 2. Write to SQLite (sole writer)
                db.insert_incident(alert)

                # 3. Dispatch to notifications
                await dispatcher.dispatch(payload)

                # 4. Update offset AFTER successful processing
                current_offset = f.tell()
                write_offset(ALERT_OFFSET_PATH, current_offset)
                processed_count += 1

                log.info(
                    "Processed alert id=%s type=%s host=%s (offset=%d)",
                    alert.get("id"),
                    alert.get("alert_type"),
                    alert.get("host"),
                    current_offset,
                )
            except sqlite3.IntegrityError as e:
                log.warning(
                    "Duplicate incident id=%s (UNIQUE constraint violation), advancing offset to prevent infinite retry: %s",
                    alert.get("id", "unknown"),
                    e,
                )
                current_offset = f.tell()
                write_offset(ALERT_OFFSET_PATH, current_offset)
                continue
            except Exception as e:
                log.error(
                    "Failed to process alert id=%s: %s",
                    alert.get("id", "unknown"),
                    e,
                    exc_info=True,
                )
                # Offset is NOT advanced on SQLite write failure — alert will be retried.
                # Note: notification dispatch failures are captured in NotificationResult,
                # not raised as exceptions, so they do not trigger this path.
                break

    if processed_count > 0:
        log.info("Processed %d alert(s) from queue", processed_count)

    return processed_count


async def _run_loop_async(
    db: Optional[Database],
    dispatcher: Optional[NotificationDispatcher],
    poll_interval: float,
) -> None:
    """Internal async loop that processes alerts continuously."""
    while True:
        try:
            # Periodic heartbeat to prove event loop is alive and not deadlocked
            HERMES_ALIVE.set(1)
            
            count = await process_alerts_async(db=db, dispatcher=dispatcher)
            if count == 0:
                await asyncio.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("Processor loop interrupted, shutting down")
            break
        except Exception as e:
            log.error("Unexpected error in processor loop: %s", e, exc_info=True)
            await asyncio.sleep(poll_interval)


def run_processor_loop(
    db: Optional[Database] = None,
    dispatcher: Optional[NotificationDispatcher] = None,
    poll_interval: float = 5.0,
) -> None:
    """
    Run the processor in a continuous loop.

    Args:
        db: Database instance (optional, creates default if None)
        dispatcher: NotificationDispatcher instance (optional, creates default if None)
        poll_interval: Seconds to wait between polling cycles when queue is empty
    """
    log.info("Starting alert processor loop (poll interval: %ss)", poll_interval)

    # Start Prometheus metrics server and set initial heartbeat
    _start_metrics_server()
    HERMES_ALIVE.set(1)

    asyncio.run(_run_loop_async(db, dispatcher, poll_interval))

    # Clear heartbeat on shutdown
    HERMES_ALIVE.set(0)
