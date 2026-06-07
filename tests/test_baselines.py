"""Tests for baselines module."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_node_ops.baselines import compute_percentiles, update_host_baselines
from agentic_node_ops.database import Database


def test_compute_percentiles_empty():
    """Test compute_percentiles returns None for empty list."""
    p50, p95 = compute_percentiles([])
    assert p50 is None
    assert p95 is None


def test_compute_percentiles_single_value():
    """Test compute_percentiles with a single value."""
    p50, p95 = compute_percentiles([42.0])
    assert p50 == 42.0
    assert p95 == 42.0


def test_compute_percentiles_even_count():
    """Test compute_percentiles with an even number of values."""
    # 10 values: 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50, p95 = compute_percentiles(values)
    assert p50 == 55.0  # median of 50 and 60
    assert p95 == 100.0  # index 9 (10 * 0.95 = 9.5 -> int is 9)


def test_compute_percentiles_odd_count():
    """Test compute_percentiles with an odd number of values."""
    # 5 values: 10, 20, 30, 40, 50
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    p50, p95 = compute_percentiles(values)
    assert p50 == 30.0
    assert p95 == 50.0  # index 4 (5 * 0.95 = 4.75 -> int is 4)


@pytest.fixture
def temp_db():
    """Provide a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_baselines.db"
        db = Database(db_path=str(db_path))
        yield db


@patch("agentic_node_ops.baselines._query_prometheus_range")
def test_update_host_baselines_success(mock_query, temp_db: Database):
    """Test update_host_baselines computes and stores percentiles correctly."""
    # Mock returns 10 values for peer_count
    mock_query.return_value = [
        10.0,
        20.0,
        30.0,
        40.0,
        50.0,
        60.0,
        70.0,
        80.0,
        90.0,
        100.0,
    ]

    results = update_host_baselines(
        db=temp_db,
        host="node-1",
        metrics=["peer_count"],
        start="now-24h",
        end="now",
    )

    assert "peer_count" in results
    p50, p95 = results["peer_count"]
    assert p50 == 55.0
    assert p95 == 100.0

    # Verify it was written to DB
    baselines = temp_db.get_host_baselines("node-1")
    assert "peer_count" in baselines
    assert baselines["peer_count"]["p50"] == 55.0
    assert baselines["peer_count"]["p95"] == 100.0


@patch("agentic_node_ops.baselines._query_prometheus_range")
def test_update_host_baselines_no_data(mock_query, temp_db: Database):
    """Test update_host_baselines handles empty Prometheus response gracefully."""
    mock_query.return_value = []

    results = update_host_baselines(
        db=temp_db,
        host="node-1",
        metrics=["peer_count"],
        start="now-24h",
        end="now",
    )

    assert "peer_count" in results
    p50, p95 = results["peer_count"]
    assert p50 is None
    assert p95 is None

    # Verify nothing was written to DB
    baselines = temp_db.get_host_baselines("node-1")
    assert "peer_count" not in baselines
