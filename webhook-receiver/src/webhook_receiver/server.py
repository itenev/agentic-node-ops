"""Webhook receiver HTTP server.

Accepts Alertmanager POST requests at /webhook, validates,
normalizes, deduplicates, applies storm protection, and appends to alerts.jsonl.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional

from aiohttp import web

from .context_fetcher import fetch_context_snapshot
from .dedup import DedupLookup, should_process
from .schema import ValidationError, validate_alertmanager_payload
from .storm_protection import StormTracker
from .types import ContextSnapshot, HermesAlert

log = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    """Parse a positive integer env var, falling back on garbage.

    Zero or negative values are dangerous here: a concurrency of 0
    deadlocks every request (Semaphore(0) never yields), a cap of 0
    rejects every payload. Misconfiguration degrades to the default
    instead of taking down ingestion.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, defaulting to %d", name, raw, default)
        return default
    if val < 1:
        log.warning("%s=%d is not positive, defaulting to %d", name, val, default)
        return default
    return val


class QueueWriter:
    """Append-only JSONL writer with O_APPEND for atomic writes."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(
            path or os.environ.get("ALERTS_JSONL_PATH", "/var/hermes/alerts.jsonl")
        )
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

    AUTH_HEADER = "X-Hermes-Token"

    def __init__(
        self,
        writer: Optional[QueueWriter] = None,
        dedup_lookup: Optional[DedupLookup] = None,
        storm_tracker: Optional[StormTracker] = None,
        auth_token: str | None = None,
        max_alerts: int | None = None,
        fetch_concurrency: int | None = None,
        fetch_timeout: int | None = None,
    ) -> None:
        self.writer = writer or QueueWriter()
        self.dedup_lookup = dedup_lookup or DedupLookup()
        self.storm_tracker = storm_tracker or StormTracker()
        # Precompute comparison bytes. surrogatepass tolerates non-UTF-8
        # bytes from os.environ and from request headers (aiohttp decodes
        # header values with surrogateescape), so a malformed token can
        # never raise here — it can only mismatch.
        self._expected_token_bytes = (
            auth_token.encode("utf-8", "surrogatepass") if auth_token else None
        )
        self.max_alerts = (
            max_alerts
            if max_alerts is not None and max_alerts > 0
            else _positive_int_env("MAX_ALERTS_PER_REQUEST", 200)
        )
        self.fetch_concurrency = (
            fetch_concurrency
            if fetch_concurrency is not None and fetch_concurrency > 0
            else _positive_int_env("CONTEXT_FETCH_CONCURRENCY", 8)
        )
        self.fetch_timeout = (
            fetch_timeout
            if fetch_timeout is not None and fetch_timeout > 0
            else _positive_int_env("CONTEXT_FETCH_TIMEOUT", 30)
        )
        # One semaphore for the process, not per request: the concurrency
        # bound must hold across concurrent webhook posts, not just within
        # one. (Lazily loop-bound on first acquire; safe to construct here.)
        self._fetch_sem = asyncio.Semaphore(self.fetch_concurrency)
        self.alerts_received = 0
        self.alerts_deduped = 0
        self.alerts_bundled = 0
        self.alerts_errors = 0
        self.auth_rejected = 0

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Validate the shared-secret token. Returns an error response or None.

        Fails closed: if no token is configured, all webhook posts are
        rejected (503) rather than accepted unauthenticated.
        """
        if self._expected_token_bytes is None:
            log.error(
                "Rejected webhook: WEBHOOK_AUTH_TOKEN is not configured (fail closed)"
            )
            self.auth_rejected += 1
            return web.json_response(
                {"error": "Authentication not configured"}, status=503
            )

        provided = request.headers.get(self.AUTH_HEADER, "")
        # sha256 digests make the comparison fixed-length, so
        # hmac.compare_digest leaks neither content nor length.
        if not hmac.compare_digest(
            hashlib.sha256(provided.encode("utf-8", "surrogatepass")).digest(),
            hashlib.sha256(self._expected_token_bytes).digest(),
        ):
            log.warning(
                "Rejected webhook from %s: invalid or missing %s header",
                request.remote,
                self.AUTH_HEADER,
            )
            self.auth_rejected += 1
            return web.json_response({"error": "Unauthorized"}, status=401)

        return None

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """POST /webhook — accepts Alertmanager webhook payloads."""
        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error

        try:
            body = await request.json()
        except json.JSONDecodeError:
            self.alerts_errors += 1
            return web.json_response({"error": "Invalid JSON"}, status=400)

        try:
            alerts = validate_alertmanager_payload(body)
        except ValidationError as e:
            self.alerts_errors += 1
            return web.json_response({"error": str(e)}, status=400)

        if not alerts:
            return web.json_response({"status": "ok", "alerts_processed": 0})

        # Hard cap: refuse absurdly large batches instead of amplifying
        # per-alert context work (Alertmanager groups are small; anything
        # above the cap is malformed or abusive).
        if len(alerts) > self.max_alerts:
            self.alerts_errors += 1
            log.warning(
                "Rejected payload with %d alerts (max %d)",
                len(alerts),
                self.max_alerts,
            )
            return web.json_response(
                {"error": f"Too many alerts in payload (max {self.max_alerts})"},
                status=413,
            )

        # Pre-fetch context snapshot for each alert.
        # fetch_context_snapshot does blocking network I/O (raw socket to
        # Docker + urllib to Prometheus) — it must never run on the event
        # loop, or one large payload stalls /health for every concurrent
        # client. Bounded worker threads + shared semaphore instead; a
        # fetch failure or timeout degrades the snapshot, never the ingest.
        async def _fetch(alert: HermesAlert) -> None:
            async with self._fetch_sem:
                try:
                    alert.context_snapshot = await asyncio.wait_for(
                        asyncio.to_thread(
                            fetch_context_snapshot,
                            host=alert.host,
                            container=alert.container,
                            client=alert.client,
                        ),
                        timeout=self.fetch_timeout,
                    )
                except Exception as e:  # noqa: BLE001 — degrade, don't drop
                    log.warning("Context fetch failed for %s: %s", alert.id, e)
                    # Fresh snapshot: never mutate/assume the current one.
                    alert.context_snapshot = ContextSnapshot(
                        container_status_note=f"context fetch failed: {type(e).__name__}"
                    )

        await asyncio.gather(*(_fetch(a) for a in alerts))

        offsets = []
        deduped_ids = []
        bundled_ids = []
        bundled_count = 0
        pending: list[HermesAlert] = []

        # Phase 1: dedup
        for alert in alerts:
            if not should_process(alert, self.dedup_lookup):
                self.alerts_deduped += 1
                deduped_ids.append(alert.id)
                continue
            pending.append(alert)

        # Phase 2: storm protection
        final_alerts: list[HermesAlert] = []
        for alert in pending:
            bundle = self.storm_tracker.check_alert(alert)
            if bundle:
                bundled_alert = bundle.to_alert()
                final_alerts.append(bundled_alert)
                self.alerts_bundled += 1
                bundled_count += 1
                bundled_ids.extend([a.id for a in bundle.alerts])
                log.warning(
                    "Storm bundle created  id=%s  type=%s  alerts=%d",
                    bundled_alert.id,
                    bundled_alert.alert_type,
                    len(bundle.alerts),
                )
            else:
                final_alerts.append(alert)

        # Phase 3: write to JSONL
        for alert in final_alerts:
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

        if deduped_ids:
            log.info("Deduplicated %d alert(s): %s", len(deduped_ids), deduped_ids)

        return web.json_response(
            {
                "status": "ok",
                "alerts_processed": len(offsets),
                "alerts_deduped": len(deduped_ids),
                "alerts_bundled": bundled_count,
                "deduped_ids": deduped_ids,
                "bundled_ids": bundled_ids,
                "offsets": offsets,
            }
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health — liveness probe."""
        return web.json_response(
            {
                "status": "healthy",
                "alerts_received": self.alerts_received,
                "alerts_deduped": self.alerts_deduped,
                "alerts_bundled": self.alerts_bundled,
                "alerts_errors": self.alerts_errors,
                "auth_rejected": self.auth_rejected,
            }
        )


def create_app(
    db_path: Optional[str] = None,
    jsonl_path: Optional[str] = None,
    auth_token: str | None = None,
    max_alerts: int | None = None,
    fetch_concurrency: int | None = None,
    fetch_timeout: int | None = None,
) -> web.Application:
    """Create and configure the aiohttp application."""
    writer = QueueWriter(path=jsonl_path)
    dedup = DedupLookup(db_path=db_path)
    storm = StormTracker()
    handler = WebhookHandler(
        writer=writer,
        dedup_lookup=dedup,
        storm_tracker=storm,
        auth_token=auth_token,
        max_alerts=max_alerts,
        fetch_concurrency=fetch_concurrency,
        fetch_timeout=fetch_timeout,
    )
    app = web.Application()
    app.router.add_post("/webhook", handler.handle_webhook)
    app.router.add_get("/health", handler.handle_health)
    app["handler"] = handler
    return app


def main() -> None:
    port = int(os.environ.get("WEBHOOK_PORT", 8090))
    db_path = os.environ.get("INCIDENTS_DB_PATH")
    jsonl_path = os.environ.get("ALERTS_JSONL_PATH")
    auth_token = os.environ.get("WEBHOOK_AUTH_TOKEN")
    logging.basicConfig(level=logging.INFO)
    if not auth_token:
        log.warning(
            "WEBHOOK_AUTH_TOKEN is not set — all webhook posts will be "
            "rejected (fail closed). Generate one with: "
            "openssl rand -hex 32"
        )
    log.info("Starting webhook receiver on port %d", port)
    app = create_app(db_path=db_path, jsonl_path=jsonl_path, auth_token=auth_token)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
