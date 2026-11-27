"""Tests for the webhook receiver HTTP server."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from webhook_receiver.server import QueueWriter, WebhookHandler, create_app


@pytest.fixture
def tmp_jsonl(tmp_path):
    """Provide a temporary JSONL path for tests."""
    return str(tmp_path / "alerts.jsonl")


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite DB with incidents table."""
    db_path = tmp_path / "incidents.db"
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
    return str(db_path)


@pytest.fixture
def app(tmp_jsonl, tmp_db):
    """Create app with temporary paths."""
    return create_app(db_path=tmp_db, jsonl_path=tmp_jsonl)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


def _valid_payload(
    alertname="consensus_desync", severity="high", host="validator-01"
) -> dict:
    """Build a valid Alertmanager webhook payload for testing."""
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "severity": severity,
                    "host": host,
                },
                "annotations": {},
                "startsAt": "2025-01-01T00:00:00Z",
            }
        ],
        "commonLabels": {},
        "externalURL": "http://alertmanager:9093",
    }


class TestHealthEndpoint:
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "healthy"
        assert "alerts_received" in data
        assert "alerts_deduped" in data
        assert "alerts_bundled" in data
        assert "alerts_errors" in data

    async def test_health_tracks_errors(self, client):
        await client.post(
            "/webhook",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        resp = await client.get("/health")
        data = await resp.json()
        assert data["alerts_errors"] >= 1


class TestWebhookEndpoint:
    async def test_valid_webhook_returns_200(self, client):
        resp = await client.post("/webhook", json=_valid_payload())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["alerts_processed"] == 1
        assert "offsets" in data

    async def test_multiple_alerts_processed(self, client):
        payload = _valid_payload()
        payload["alerts"].append(
            {
                "status": "firing",
                "labels": {
                    "alertname": "validator_duty_miss",
                    "severity": "critical",
                    "host": "validator-02",
                },
                "annotations": {},
                "startsAt": "2025-01-01T00:00:00Z",
            }
        )
        resp = await client.post("/webhook", json=payload)
        data = await resp.json()
        assert data["alerts_processed"] == 2

    async def test_invalid_json_returns_400(self, client):
        resp = await client.post(
            "/webhook",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_missing_alerts_returns_400(self, client):
        resp = await client.post("/webhook", json={"version": "4", "no_alerts": True})
        assert resp.status == 400

    async def test_missing_alertname_returns_400(self, client):
        payload = _valid_payload()
        payload["alerts"][0]["labels"].pop("alertname")
        resp = await client.post("/webhook", json=payload)
        assert resp.status == 400

    async def test_get_returns_405(self, client):
        resp = await client.get("/webhook")
        assert resp.status == 405

    async def test_alerts_written_to_jsonl(self, client, tmp_jsonl):
        """Verify that accepted alerts are actually written to the JSONL file."""
        resp = await client.post("/webhook", json=_valid_payload())
        assert resp.status == 200

        path = Path(tmp_jsonl)
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["alert_type"] == "consensus_desync"
        assert data["severity"] == "high"
        assert data["host"] == "validator-01"


def _seed_incident(db_path, alert_type, host, severity, fired_at, resolved_at=None):
    """Insert a prior incident into the test DB."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO incidents (id, alert_type, host, severity, fired_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            f"test-{alert_type}-{host}",
            alert_type,
            host,
            severity,
            fired_at,
            resolved_at,
        ),
    )
    conn.commit()
    conn.close()


class TestDedupIntegration:
    """Test dedup logic integrated into the webhook endpoint."""

    async def test_dedup_suppresses_duplicate_within_cooldown(
        self, client, tmp_db, tmp_jsonl
    ):
        """Alert within cooldown window of a prior incident should be deduped."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _seed_incident(
            tmp_db,
            "consensus_desync",
            "validator-01",
            "high",
            recent,  # within 1-hour cooldown for high severity
        )
        resp = await client.post("/webhook", json=_valid_payload())
        data = await resp.json()
        assert data["alerts_deduped"] == 1
        assert data["alerts_processed"] == 0

    async def test_dedup_allows_higher_severity(self, client, tmp_db):
        """A critical alert should pass through even if a high one was recently processed."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _seed_incident(tmp_db, "consensus_desync", "validator-01", "high", recent)
        resp = await client.post(
            "/webhook",
            json=_valid_payload(severity="critical"),
        )
        data = await resp.json()
        assert data["alerts_processed"] == 1
        assert data["alerts_deduped"] == 0

    async def test_dedup_allows_resolved_to_firing(self, client, tmp_db):
        """A new firing alert after a resolved one should pass."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        resolved = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
        _seed_incident(
            tmp_db,
            "consensus_desync",
            "validator-01",
            "high",
            recent,
            resolved,  # resolved
        )
        resp = await client.post("/webhook", json=_valid_payload())
        data = await resp.json()
        assert data["alerts_processed"] == 1
        assert data["alerts_deduped"] == 0

    async def test_dedup_allows_different_host(self, client, tmp_db):
        """Same alert type on a different host should not be deduped."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _seed_incident(tmp_db, "consensus_desync", "validator-01", "high", recent)
        resp = await client.post(
            "/webhook",
            json=_valid_payload(host="validator-02"),
        )
        data = await resp.json()
        assert data["alerts_processed"] == 1
        assert data["alerts_deduped"] == 0

    async def test_health_tracks_dedup_count(self, client, tmp_db):
        """Health endpoint should reflect deduped alert count."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _seed_incident(tmp_db, "consensus_desync", "validator-01", "high", recent)
        await client.post("/webhook", json=_valid_payload())
        resp = await client.get("/health")
        data = await resp.json()
        assert data["alerts_deduped"] == 1


class TestStormProtectionIntegration:
    """Test storm protection integrated into the webhook endpoint."""

    async def test_single_host_storm_bundles_alerts(self, client, tmp_jsonl):
        """>3 alerts on same host within 30s should be bundled."""
        payload = _valid_payload()
        # Send 4 alerts on the same host
        for _ in range(4):
            resp = await client.post("/webhook", json=payload)

        data = await resp.json()
        assert data["alerts_bundled"] >= 1
        # The last alert triggered a bundle, so it's 1 bundled alert
        # written to JSONL (not 4 individual)
        path = Path(tmp_jsonl)
        lines = path.read_text().strip().split("\n")
        # 3 individual + 1 bundle = 4 lines (or could be 3 + 1 bundle = 4)
        # Actually: first 3 go through, 4th triggers bundle → 4 lines total
        assert len(lines) == 4
        # Last line should be the storm bundle
        bundle = json.loads(lines[-1])
        assert bundle["alert_type"] == "storm_single_host"

    async def test_cross_host_storm_bundles_alerts(self, client, tmp_jsonl):
        """Same alert type on >=2 hosts within 60s should be bundled."""
        _ = await client.post("/webhook", json=_valid_payload(host="validator-01"))
        resp2 = await client.post("/webhook", json=_valid_payload(host="validator-02"))

        data2 = await resp2.json()
        assert data2["alerts_bundled"] >= 1

        path = Path(tmp_jsonl)
        lines = path.read_text().strip().split("\n")
        # First alert goes through, second triggers bundle
        assert len(lines) == 2
        bundle = json.loads(lines[-1])
        assert bundle["alert_type"] == "storm_cross_host"

    async def test_health_tracks_bundle_count(self, client):
        """Health endpoint should reflect bundled alert count."""
        # Trigger a cross-host storm
        await client.post("/webhook", json=_valid_payload(host="validator-01"))
        await client.post("/webhook", json=_valid_payload(host="validator-02"))

        resp = await client.get("/health")
        data = await resp.json()
        assert data["alerts_bundled"] >= 1

    async def test_response_includes_bundled_ids(self, client):
        """Response should include IDs of alerts that were bundled."""
        await client.post("/webhook", json=_valid_payload(host="validator-01"))
        resp = await client.post("/webhook", json=_valid_payload(host="validator-02"))

        data = await resp.json()
        assert "bundled_ids" in data
        assert len(data["bundled_ids"]) >= 1


class TestContextFetching:
    """Context snapshot fetching must never block or break the ingest path."""

    async def test_context_fetched_off_event_loop(self, client, monkeypatch):
        """The blocking fetcher must run in a worker thread, not the loop."""
        import threading

        from webhook_receiver.types import ContextSnapshot

        threads = []
        loop_thread = threading.get_ident()

        def fake_fetch(host, container=None, client=None):
            threads.append(threading.get_ident())
            return ContextSnapshot(container_status="running")

        monkeypatch.setattr(
            "webhook_receiver.server.fetch_context_snapshot", fake_fetch
        )
        resp = await client.post("/webhook", json=_valid_payload())
        assert resp.status == 200
        assert threads and all(t != loop_thread for t in threads)

    async def test_context_fetch_failure_degrades_not_drops(
        self, client, tmp_jsonl, monkeypatch
    ):
        """A raising fetcher must degrade the snapshot, never the alert."""

        def exploding_fetch(host, container=None, client=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "webhook_receiver.server.fetch_context_snapshot", exploding_fetch
        )
        resp = await client.post("/webhook", json=_valid_payload())
        assert resp.status == 200
        data = await resp.json()
        assert data["alerts_processed"] == 1

        line = json.loads(Path(tmp_jsonl).read_text().strip())
        note = line["context_snapshot"].get("container_status_note", "")
        assert "context fetch failed" in note

    async def test_fetch_concurrency_is_bounded(
        self, aiohttp_client, monkeypatch, tmp_jsonl, tmp_db
    ):
        """At most `fetch_concurrency` fetchers may be in flight at once."""
        import threading
        import time as _time

        from webhook_receiver.types import ContextSnapshot

        lock = threading.Lock()
        state = {"in_flight": 0, "max_in_flight": 0}

        def slow_fetch(host, container=None, client=None):
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            _time.sleep(0.05)
            with lock:
                state["in_flight"] -= 1
            return ContextSnapshot()

        monkeypatch.setattr(
            "webhook_receiver.server.fetch_context_snapshot", slow_fetch
        )
        app_capped = create_app(
            db_path=tmp_db,
            jsonl_path=tmp_jsonl,
            fetch_concurrency=2,
        )
        client = await aiohttp_client(app_capped)

        payload = _valid_payload()
        payload["alerts"] = payload["alerts"] * 6  # 6 fetches, cap 2
        resp = await client.post("/webhook", json=payload)
        assert resp.status == 200
        assert state["max_in_flight"] <= 2

    async def test_payload_over_cap_returns_413(
        self, aiohttp_client, tmp_jsonl, tmp_db
    ):
        """Absurdly large batches must be refused before any context work."""
        app_capped = create_app(db_path=tmp_db, jsonl_path=tmp_jsonl, max_alerts=2)
        client = await aiohttp_client(app_capped)

        payload = _valid_payload()
        payload["alerts"] = payload["alerts"] * 3  # 3 alerts, cap 2
        resp = await client.post("/webhook", json=payload)
        assert resp.status == 413
        data = await resp.json()
        assert "max 2" in data["error"]

        health = await client.get("/health")
        health_data = await health.json()
        assert health_data["alerts_errors"] == 1
        assert health_data["alerts_received"] == 0
        assert not Path(tmp_jsonl).exists()


class TestFetchConfigParsing:
    """Env misconfiguration must degrade to defaults, never deadlock or
    reject everything (concurrency 0 hangs Semaphore; cap 0 rejects all)."""

    def test_env_concurrency_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_FETCH_CONCURRENCY", "0")
        handler = WebhookHandler(
            writer=QueueWriter(path="/dev/null"), fetch_concurrency=None
        )
        assert handler.fetch_concurrency == 8

    def test_env_concurrency_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_FETCH_CONCURRENCY", "abc")
        handler = WebhookHandler(
            writer=QueueWriter(path="/dev/null"), fetch_concurrency=None
        )
        assert handler.fetch_concurrency == 8

    def test_env_max_alerts_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv("MAX_ALERTS_PER_REQUEST", "0")
        handler = WebhookHandler(writer=QueueWriter(path="/dev/null"), max_alerts=None)
        assert handler.max_alerts == 200

    def test_explicit_zero_params_fall_back_to_env_defaults(self, monkeypatch):
        monkeypatch.delenv("MAX_ALERTS_PER_REQUEST", raising=False)
        monkeypatch.delenv("CONTEXT_FETCH_CONCURRENCY", raising=False)
        handler = WebhookHandler(
            writer=QueueWriter(path="/dev/null"),
            max_alerts=0,
            fetch_concurrency=0,
        )
        assert handler.max_alerts == 200
        assert handler.fetch_concurrency == 8

    def test_valid_explicit_params_respected(self):
        handler = WebhookHandler(
            writer=QueueWriter(path="/dev/null"),
            max_alerts=3,
            fetch_concurrency=2,
        )
        assert handler.max_alerts == 3
        assert handler.fetch_concurrency == 2

    async def test_concurrency_bound_is_global_not_per_request(
        self, aiohttp_client, monkeypatch, tmp_jsonl, tmp_db
    ):
        """The semaphore is shared across requests: two concurrent posts
        together must not exceed the bound."""
        import asyncio as _asyncio
        import threading
        import time as _time

        from webhook_receiver.types import ContextSnapshot

        lock = threading.Lock()
        state = {"in_flight": 0, "max_in_flight": 0}

        def slow_fetch(host, container=None, client=None):
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            _time.sleep(0.05)
            with lock:
                state["in_flight"] -= 1
            return ContextSnapshot()

        monkeypatch.setattr(
            "webhook_receiver.server.fetch_context_snapshot", slow_fetch
        )
        app_capped = create_app(
            db_path=tmp_db, jsonl_path=tmp_jsonl, fetch_concurrency=1
        )
        client = await aiohttp_client(app_capped)

        posts = [
            client.post("/webhook", json=_valid_payload()),
            client.post("/webhook", json=_valid_payload()),
        ]
        results = await _asyncio.gather(*posts)
        assert all(r.status == 200 for r in results)
        assert state["max_in_flight"] == 1

    async def test_hung_fetch_times_out_and_degrades(
        self, aiohttp_client, monkeypatch, tmp_jsonl, tmp_db
    ):
        """A hanging fetch must not hold the response forever: the
        wait_for deadline degrades the snapshot and the alert proceeds."""
        import time as _time

        from webhook_receiver.types import ContextSnapshot

        def hung_fetch(host, container=None, client=None):
            _time.sleep(3.0)
            return ContextSnapshot()

        monkeypatch.setattr(
            "webhook_receiver.server.fetch_context_snapshot", hung_fetch
        )
        app_timeout = create_app(db_path=tmp_db, jsonl_path=tmp_jsonl, fetch_timeout=1)
        client = await aiohttp_client(app_timeout)

        resp = await client.post("/webhook", json=_valid_payload())
        assert resp.status == 200
        data = await resp.json()
        assert data["alerts_processed"] == 1

        line = json.loads(Path(tmp_jsonl).read_text().strip())
        note = line["context_snapshot"].get("container_status_note", "")
        assert "context fetch failed" in note
