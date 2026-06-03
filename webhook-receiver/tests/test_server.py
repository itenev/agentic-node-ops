"""Tests for the webhook receiver HTTP server."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from webhook_receiver.server import create_app, WebhookHandler, QueueWriter
from webhook_receiver.types import Severity, AlertStatus


@pytest.fixture
def tmp_jsonl(tmp_path):
    """Provide a temporary JSONL path for tests."""
    return str(tmp_path / "alerts.jsonl")


@pytest.fixture
def app(tmp_jsonl):
    """Create app with a temporary JSONL path."""
    old = os.environ.get("ALERTS_JSONL_PATH")
    os.environ["ALERTS_JSONL_PATH"] = tmp_jsonl
    try:
        yield create_app()
    finally:
        if old is None:
            os.environ.pop("ALERTS_JSONL_PATH", None)
        else:
            os.environ["ALERTS_JSONL_PATH"] = old


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


class TestHealthEndpoint:
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "healthy"
        assert "alerts_received" in data
        assert "alerts_errors" in data

    async def test_health_tracks_errors(self, client):
        # Send invalid JSON to trigger an error
        await client.post(
            "/webhook",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        resp = await client.get("/health")
        data = await resp.json()
        assert data["alerts_errors"] >= 1


class TestWebhookEndpoint:
    def _valid_payload(self, alertname="consensus_desync", severity="high") -> dict:
        return {
            "version": "4",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": alertname,
                        "severity": severity,
                        "host": "validator-01",
                    },
                    "annotations": {},
                    "startsAt": "2025-01-01T00:00:00Z",
                }
            ],
            "commonLabels": {},
            "externalURL": "http://alertmanager:9093",
        }

    async def test_valid_webhook_returns_200(self, client):
        resp = await client.post(
            "/webhook", json=self._valid_payload()
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["alerts_processed"] == 1
        assert "offsets" in data

    async def test_multiple_alerts_processed(self, client):
        payload = self._valid_payload()
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
        resp = await client.post(
            "/webhook", json={"version": "4", "no_alerts": True}
        )
        assert resp.status == 400

    async def test_missing_alertname_returns_400(self, client):
        payload = self._valid_payload()
        payload["alerts"][0]["labels"].pop("alertname")
        resp = await client.post("/webhook", json=payload)
        assert resp.status == 400

    async def test_get_returns_405(self, client):
        resp = await client.get("/webhook")
        assert resp.status == 405

    async def test_alerts_written_to_jsonl(self, client, tmp_jsonl):
        """Verify that accepted alerts are actually written to the JSONL file."""
        resp = await client.post("/webhook", json=self._valid_payload())
        assert resp.status == 200

        path = Path(tmp_jsonl)
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["alert_type"] == "consensus_desync"
        assert data["severity"] == "high"
        assert data["host"] == "validator-01"
