"""Tests for processor module."""

import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from agentic_node_ops.processor import (
    read_offset,
    write_offset,
    process_alerts_async,
    _build_payload,
)
from agentic_node_ops.types import Severity


def test_read_offset_missing():
    """Test read_offset returns 0 when file is missing."""
    assert read_offset("/tmp/nonexistent_offset_file_12345.txt") == 0


def test_read_offset_valid():
    """Test read_offset returns integer from file."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("42")
        path = f.name
    try:
        assert read_offset(path) == 42
    finally:
        os.unlink(path)


def test_write_offset_atomic():
    """Test write_offset writes atomically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "offset.txt")
        write_offset(path, 100)
        assert read_offset(path) == 100

        # Verify no .tmp file left behind
        assert not os.path.exists(path + ".tmp")


def test_build_payload():
    """Test _build_payload converts dict to NotificationPayload."""
    alert = {
        "id": "evt_123",
        "alert_type": "consensus_desync",
        "severity": "critical",
        "host": "validator-01",
        "context_snapshot": {"peer_count": 2},
        "runbook_hint": "consensus_desync",
    }

    payload = _build_payload(alert)

    assert payload.incident_id == "evt_123"
    assert payload.alert_type == "consensus_desync"
    assert payload.severity == Severity.CRITICAL
    assert payload.host == "validator-01"
    assert payload.diagnostics == {"peer_count": 2}
    assert payload.runbook_id == "consensus_desync"


@pytest.mark.asyncio
async def test_process_alerts_async_empty():
    """Test process_alerts_async returns 0 when no jsonl exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "alerts.jsonl")
        offset_path = os.path.join(tmpdir, "offset.txt")
        db_path = os.path.join(tmpdir, "incidents.db")

        with (
            patch("agentic_node_ops.processor.ALERTS_JSONL_PATH", jsonl_path),
            patch("agentic_node_ops.processor.ALERT_OFFSET_PATH", offset_path),
        ):
            from agentic_node_ops.database import Database
            from agentic_node_ops.dispatcher import NotificationDispatcher

            db = Database(db_path=db_path)
            mock_dispatcher = MagicMock(spec=NotificationDispatcher)

            count = await process_alerts_async(db=db, dispatcher=mock_dispatcher)
            assert count == 0


@pytest.mark.asyncio
async def test_process_alerts_async_success():
    """Test process_alerts_async processes alerts and updates offset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "alerts.jsonl")
        offset_path = os.path.join(tmpdir, "offset.txt")
        db_path = os.path.join(tmpdir, "incidents.db")

        # Create test alert
        alert = {
            "id": "evt_999",
            "alert_type": "test_alert",
            "severity": "high",
            "host": "test-host",
            "fired_at": "2025-01-01T00:00:00Z",
            "context_snapshot": {},
        }

        with open(jsonl_path, "w") as f:
            f.write(json.dumps(alert) + "\n")

        with (
            patch("agentic_node_ops.processor.ALERTS_JSONL_PATH", jsonl_path),
            patch("agentic_node_ops.processor.ALERT_OFFSET_PATH", offset_path),
        ):
            from agentic_node_ops.database import Database
            from agentic_node_ops.dispatcher import NotificationDispatcher

            # Mock dispatcher to avoid actual network calls
            mock_dispatcher = MagicMock(spec=NotificationDispatcher)
            mock_dispatcher.dispatch = AsyncMock(return_value=[])

            db = Database(db_path=db_path)

            count = await process_alerts_async(db=db, dispatcher=mock_dispatcher)

            assert count == 1
            assert read_offset(offset_path) > 0

            # Verify it was written to DB
            last = db.get_last_processed("test_alert", "test-host")
            assert last is not None
            assert last["id"] == "evt_999"


@pytest.mark.asyncio
async def test_process_alerts_async_malformed_json():
    """Test process_alerts_async skips malformed JSON and advances offset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "alerts.jsonl")
        offset_path = os.path.join(tmpdir, "offset.txt")
        db_path = os.path.join(tmpdir, "incidents.db")

        with open(jsonl_path, "w") as f:
            f.write("not valid json\n")
            f.write(
                '{"id": "evt_good", "alert_type": "test", "severity": "low", "host": "h1", "fired_at": "2025-01-01T00:00:00Z", "context_snapshot": {}}\n'
            )

        with (
            patch("agentic_node_ops.processor.ALERTS_JSONL_PATH", jsonl_path),
            patch("agentic_node_ops.processor.ALERT_OFFSET_PATH", offset_path),
        ):
            from agentic_node_ops.database import Database
            from agentic_node_ops.dispatcher import NotificationDispatcher

            mock_dispatcher = MagicMock(spec=NotificationDispatcher)
            mock_dispatcher.dispatch = AsyncMock(return_value=[])

            db = Database(db_path=db_path)

            count = await process_alerts_async(db=db, dispatcher=mock_dispatcher)

            # Should skip the bad line and process the good one
            assert count == 1

            last = db.get_last_processed("test", "h1")
            assert last is not None
            assert last["id"] == "evt_good"


@pytest.mark.asyncio
async def test_process_alerts_async_duplicate_incident():
    """Test process_alerts_async handles UNIQUE constraint violations by advancing offset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "alerts.jsonl")
        offset_path = os.path.join(tmpdir, "offset.txt")
        db_path = os.path.join(tmpdir, "incidents.db")

        # Create two identical alerts (same ID)
        alert = {
            "id": "evt_dup",
            "alert_type": "test_alert",
            "severity": "high",
            "host": "test-host",
            "fired_at": "2025-01-01T00:00:00Z",
            "context_snapshot": {},
        }

        with open(jsonl_path, "w") as f:
            f.write(json.dumps(alert) + "\n")
            f.write(json.dumps(alert) + "\n")

        with (
            patch("agentic_node_ops.processor.ALERTS_JSONL_PATH", jsonl_path),
            patch("agentic_node_ops.processor.ALERT_OFFSET_PATH", offset_path),
        ):
            from agentic_node_ops.database import Database
            from agentic_node_ops.dispatcher import NotificationDispatcher

            mock_dispatcher = MagicMock(spec=NotificationDispatcher)
            mock_dispatcher.dispatch = AsyncMock(return_value=[])

            db = Database(db_path=db_path)

            count = await process_alerts_async(db=db, dispatcher=mock_dispatcher)

            # First alert processes, second hits UNIQUE constraint and is skipped
            assert count == 1

            # Offset should have advanced past BOTH lines
            assert read_offset(offset_path) > 0


@pytest.mark.asyncio
async def test_process_alerts_async_wires_hermes_context():
    """Test process_alerts_async calls build_hermes_context to populate summary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "alerts.jsonl")
        offset_path = os.path.join(tmpdir, "offset.txt")
        db_path = os.path.join(tmpdir, "incidents.db")

        alert = {
            "id": "evt_ctx",
            "alert_type": "context_test",
            "severity": "medium",
            "host": "ctx-host",
            "fired_at": "2025-01-01T00:00:00Z",
            "context_snapshot": {"peer_count": 5},
        }

        with open(jsonl_path, "w") as f:
            f.write(json.dumps(alert) + "\n")

        with (
            patch("agentic_node_ops.processor.ALERTS_JSONL_PATH", jsonl_path),
            patch("agentic_node_ops.processor.ALERT_OFFSET_PATH", offset_path),
        ):
            from agentic_node_ops.database import Database
            from agentic_node_ops.dispatcher import NotificationDispatcher

            mock_dispatcher = MagicMock(spec=NotificationDispatcher)

            # Capture the payload passed to dispatch
            captured_payloads = []

            async def capture_dispatch(payload):
                captured_payloads.append(payload)
                return []

            mock_dispatcher.dispatch = capture_dispatch

            db = Database(db_path=db_path)

            count = await process_alerts_async(db=db, dispatcher=mock_dispatcher)

            assert count == 1
            assert len(captured_payloads) == 1

            payload = captured_payloads[0]
            # Verify summary is populated by build_hermes_context, not the placeholder
            assert payload.summary != "Alert received. Hermes analysis pending."
            assert "context_test" in payload.summary
            assert "ctx-host" in payload.summary
            assert "CURRENT STATE:" in payload.summary
