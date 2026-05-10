"""
hermes/notifications/dispatcher.py

Routes a NotificationPayload to the correct channels based on severity.

    critical  →  Discord + ntfy  (both fire)
    high      →  Discord only
    medium    →  Discord only
    low       →  Discord only

The dispatcher is the only place that knows about routing — callers
just build a payload and call dispatch(). Adding a new channel (email,
Slack, PagerDuty) means adding it here and nowhere else.
"""

import asyncio
import logging
import os
from typing import Optional

from .discord import DiscordNotifier
from .ntfy    import NtfyNotifier
from .types   import CHANNEL_ROUTING, NotificationPayload, NotificationResult, Severity

log = logging.getLogger(__name__)


class NotificationDispatcher:
    """
    Central dispatcher. Instantiate once at startup and reuse.

    Config is read from environment variables so nothing sensitive
    lives in code or config files:

        DISCORD_WEBHOOK_URL   required
        NTFY_TOPIC            required
        NTFY_SERVER_URL       optional (defaults to https://ntfy.sh)
        NTFY_USERNAME         optional (for authenticated self-hosted ntfy)
        NTFY_PASSWORD         optional
    """

    def __init__(
        self,
        discord_webhook_url: Optional[str] = None,
        ntfy_topic:          Optional[str] = None,
        ntfy_server_url:     Optional[str] = None,
        ntfy_username:       Optional[str] = None,
        ntfy_password:       Optional[str] = None,
    ) -> None:
        webhook = discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
        topic   = ntfy_topic          or os.environ.get("NTFY_TOPIC", "")

        if not webhook:
            raise ValueError("DISCORD_WEBHOOK_URL is required")
        if not topic:
            raise ValueError("NTFY_TOPIC is required")

        self._discord = DiscordNotifier(webhook_url=webhook)
        self._ntfy    = NtfyNotifier(
            topic      = topic,
            server_url = ntfy_server_url or os.environ.get("NTFY_SERVER_URL", "https://ntfy.sh"),
            username   = ntfy_username   or os.environ.get("NTFY_USERNAME"),
            password   = ntfy_password   or os.environ.get("NTFY_PASSWORD"),
        )

    async def dispatch(self, payload: NotificationPayload) -> list[NotificationResult]:
        """
        Fire all appropriate channels concurrently.
        Never raises — failures are captured in NotificationResult.error.
        """
        channels = CHANNEL_ROUTING[payload.severity]

        # Slashing risk always gets both channels regardless of severity mapping
        if payload.is_slashing_risk and "ntfy" not in channels:
            channels = list(channels) + ["ntfy"]

        tasks = []
        for channel in channels:
            if channel == "discord":
                tasks.append(self._discord.send(payload))
            elif channel == "ntfy":
                tasks.append(self._ntfy.send(payload))

        results: list[NotificationResult] = await asyncio.gather(*tasks)

        for r in results:
            if not r.success:
                log.error(
                    "Notification failed  channel=%s  incident=%s  error=%s",
                    r.channel, payload.incident_id, r.error,
                )
            else:
                log.info(
                    "Notification sent  channel=%s  incident=%s  message_id=%s",
                    r.channel, payload.incident_id, r.message_id,
                )

        return results
