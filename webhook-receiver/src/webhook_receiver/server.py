"""Webhook receiver HTTP server.

Accepts Alertmanager POST requests at /webhook, validates,
normalizes, and appends to alerts.jsonl.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from .schema import ValidationError, validate_alertmanager_payload
from .types import HermesAlert

log = logging.getLogger(__name__)


class QueueWriter:
    """Append-only JSONL writer with O_APPEND for atomic writes."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or os.environ.get("ALERTS_JSONL_PATH", "/var/hermes/alerts.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, alert: HermesAlert) -> int:
        """Append a single alert as a JSON line. Returns byte offset."""
        line = alert.to_json() + "\n"
        with open(self.path, "a") as f:
            offset = f.tell()
            f.write(line)
        return offset


class WebhookHandler:
    """Handles POST /webhook requests from Alertmanager."""

    def __init__(self, writer: Optional[QueueWriter] = None) -> None:
        self.writer = writer or QueueWriter()
        self.alerts_received = 0
        self.alerts_errors = 0

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """POST /webhook — accepts Alertmanager webhook payloads."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            self.alerts_errors += 1
            return web.json_response(
                {"error": "Invalid JSON"}, status=400
            )

        try:
            alerts = validate_alertmanager_payload(body)
        except ValidationError as e:
            self.alerts_errors += 1
            return web.json_response(
                {"error": str(e)}, status=400
            )

        if not alerts:
            return web.json_response({"status": "ok", "alerts_processed": 0})

        offsets = []
        for alert in alerts:
            offset = self.writer.append(alert)
            offsets.append(offset)
            self.alerts_received += 1
            log.info(
                "Alert received  id=%s  type=%s  severity=%s  host=%s  offset=%d",
                alert.id,
                alert.alert_type,
                alert.severity.value,
                alert.host,
                offset,
            )

        return web.json_response(
            {
                "status": "ok",
                "alerts_processed": len(alerts),
                "offsets": offsets,
            }
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health — liveness probe."""
        return web.json_response(
            {
                "status": "healthy",
                "alerts_received": self.alerts_received,
                "alerts_errors": self.alerts_errors,
            }
        )


def create_app() -> web.Application:
    """Create and configure the aiohttp application."""
    handler = WebhookHandler()
    app = web.Application()
    app.router.add_post("/webhook", handler.handle_webhook)
    app.router.add_get("/health", handler.handle_health)
    app["handler"] = handler
    return app


def main() -> None:
    port = int(os.environ.get("WEBHOOK_PORT", 8090))
    logging.basicConfig(level=logging.INFO)
    log.info("Starting webhook receiver on port %d", port)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
