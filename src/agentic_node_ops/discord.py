"""
hermes/notifications/discord.py

Discord notification channel — webhook-based (Phase 1-4).

Phase 5 note: when the approval flow arrives, swap _send_webhook()
for a Bot API call using the same payload shape. The NotificationPayload
contract and formatting logic stay identical; only the transport changes.

Discord webhook docs: https://discord.com/developers/docs/resources/webhook
Embed structure: https://discord.com/developers/docs/resources/message#embed-object
"""

import logging
from typing import Optional

import httpx

from .types import (
    DISCORD_COLOR,
    SEVERITY_EMOJI,
    NotificationPayload,
    NotificationResult,
    Severity,
)

log = logging.getLogger(__name__)

# Maximum lengths enforced by Discord API
_EMBED_TITLE_MAX = 256
_EMBED_DESCRIPTION_MAX = 4096
_FIELD_NAME_MAX = 256
_FIELD_VALUE_MAX = 1024
_EMBED_TOTAL_MAX = 6000


class DiscordNotifier:
    """
    Sends rich embed messages to a Discord channel via webhook.

    Usage:
        notifier = DiscordNotifier(webhook_url=os.environ["DISCORD_WEBHOOK_URL"])
        result   = await notifier.send(payload)
    """

    def __init__(self, webhook_url: str, timeout: float = 10.0) -> None:
        self._url = webhook_url.rstrip("/")
        self._timeout = timeout

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        embed = self._build_embed(payload)
        content = self._build_content(payload)

        try:
            message_id = await self._send_webhook(content=content, embeds=[embed])
            return NotificationResult(
                channel="discord", success=True, message_id=message_id
            )
        except Exception as exc:
            log.exception(
                "Discord notification failed for incident %s", payload.incident_id
            )
            return NotificationResult(channel="discord", success=False, error=str(exc))

    # ------------------------------------------------------------------ #
    # Embed construction                                                   #
    # ------------------------------------------------------------------ #

    def _build_embed(self, p: NotificationPayload) -> dict:
        emoji = SEVERITY_EMOJI[p.severity]
        color = DISCORD_COLOR[p.severity]

        title = _truncate(f"{emoji} {p.title}", _EMBED_TITLE_MAX)
        description = _truncate(p.summary, _EMBED_DESCRIPTION_MAX)

        fields = []

        # Diagnostic key/value pairs as inline fields
        for label, value in p.diagnostics.items():
            fields.append(
                {
                    "name": _truncate(label, _FIELD_NAME_MAX),
                    "value": _truncate(f"`{value}`", _FIELD_VALUE_MAX),
                    "inline": True,
                }
            )

        # Proposed action block (Phase 2+: text only; Phase 5+: approval buttons)
        if p.proposed_action:
            action_text = p.proposed_action
            if p.approval_required:
                action_text += (
                    "\n> ⏳ Awaiting your approval — reply **approve** or **skip**"
                )
            fields.append(
                {
                    "name": "Proposed action",
                    "value": _truncate(action_text, _FIELD_VALUE_MAX),
                    "inline": False,
                }
            )

        # Slashing: prominent forensic evidence path
        if p.is_slashing_risk and p.forensic_path:
            fields.append(
                {
                    "name": "⚠️ Evidence bundle",
                    "value": _truncate(f"`{p.forensic_path}`", _FIELD_VALUE_MAX),
                    "inline": False,
                }
            )

        embed: dict = {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"incident {p.incident_id}  ·  host: {p.host}"
                + (f"  ·  runbook: {p.runbook_id}" if p.runbook_id else ""),
            },
        }

        return embed

    def _build_content(self, p: NotificationPayload) -> Optional[str]:
        """
        Plain-text content above the embed.
        Used for @here / @everyone on critical alerts so Discord
        sends a push notification even with the channel muted.
        (ntfy handles the true silent-mode bypass; this is belt-and-suspenders.)
        """
        if p.severity == Severity.CRITICAL or p.is_slashing_risk:
            return "@here"
        return None

    # ------------------------------------------------------------------ #
    # Transport                                                            #
    # ------------------------------------------------------------------ #

    async def _send_webhook(
        self,
        content: Optional[str],
        embeds: list[dict],
    ) -> Optional[str]:
        """
        POST to the Discord webhook endpoint.
        Returns the Discord message ID on success.

        To switch to Bot API in Phase 5, replace this method body with:
            POST /channels/{channel_id}/messages
            Authorization: Bot {token}
        The payload shape (content + embeds) is identical.
        """
        body: dict = {"embeds": embeds}
        if content:
            body["content"] = content

        # ?wait=true makes Discord return the created message object,
        # giving us the message ID for future edits (e.g. marking resolved).
        url = f"{self._url}?wait=true"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body)

        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Discord webhook returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        return data.get("id")

    # ------------------------------------------------------------------ #
    # Lifecycle helpers (Phase 5+)                                         #
    # ------------------------------------------------------------------ #

    async def update_message(
        self,
        message_id: str,
        resolved: bool,
        outcome_note: Optional[str] = None,
    ) -> bool:
        """
        Edit a previously sent embed to show resolved/failed status.
        Requires the message_id returned from send().

        Only works with Bot API (not webhooks without the message ID).
        Phase 5: replace stub with PATCH /webhooks/{id}/{token}/messages/{msg_id}
        """
        # Phase 1-4 stub — log and return
        log.info(
            "update_message called (stub) — message_id=%s resolved=%s note=%s",
            message_id,
            resolved,
            outcome_note,
        )
        return True


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
