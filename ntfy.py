"""
hermes/notifications/ntfy.py

Tier-2 critical alert channel via ntfy.sh.

ntfy bypasses iOS Do Not Disturb and Android priority settings when
Priority: urgent is set. This is the primary "wake the operator" mechanism.

Self-hosting: set NTFY_SERVER_URL to your own ntfy instance.
Public instance: https://ntfy.sh (free, topic name = secret, no auth by default).

ntfy docs: https://docs.ntfy.sh/publish/
"""

import logging
from typing import Optional

import httpx

from .types import NTFY_PRIORITY, NotificationPayload, NotificationResult, Severity

log = logging.getLogger(__name__)

_DEFAULT_SERVER = "https://ntfy.sh"

# ntfy action buttons (Phase 5+: approval via ntfy actions)
# https://docs.ntfy.sh/publish/#action-buttons
_APPROVE_ACTION = "http, Approve, {callback_url}/approve, method=POST, clear=true"
_SKIP_ACTION    = "http, Skip, {callback_url}/skip, method=POST, clear=true"


class NtfyNotifier:
    """
    Sends urgent push notifications via ntfy.sh.

    The topic name acts as the shared secret — use a long random string,
    not something guessable. Nobody but you should know it.

    Usage:
        notifier = NtfyNotifier(
            topic=os.environ["NTFY_TOPIC"],          # e.g. "hermes-xk92mq7p"
            server_url=os.environ.get("NTFY_SERVER_URL"),  # optional, defaults to ntfy.sh
        )
        result = await notifier.send(payload)
    """

    def __init__(
        self,
        topic: str,
        server_url: str = _DEFAULT_SERVER,
        timeout: float  = 10.0,
        # Optional: set these if your ntfy instance requires auth
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._url     = f"{server_url.rstrip('/')}/{topic}"
        self._timeout = timeout
        self._auth    = (username, password) if username else None

    async def send(self, payload: NotificationPayload) -> NotificationResult:
        try:
            await self._post(payload)
            return NotificationResult(channel="ntfy", success=True)
        except Exception as exc:
            log.exception("ntfy notification failed for incident %s", payload.incident_id)
            return NotificationResult(channel="ntfy", success=False, error=str(exc))

    async def _post(self, p: NotificationPayload) -> None:
        title   = _build_title(p)
        message = _build_message(p)
        tags    = _build_tags(p)
        priority = NTFY_PRIORITY[p.severity]

        headers = {
            "Title":    title,
            "Priority": priority,
            "Tags":     ",".join(tags),
        }

        # Phase 5: add action buttons so operator can approve from notification
        # Uncomment when callback URL is available:
        #
        # if p.approval_required and callback_url:
        #     headers["Actions"] = "; ".join([
        #         _APPROVE_ACTION.format(callback_url=callback_url),
        #         _SKIP_ACTION.format(callback_url=callback_url),
        #     ])

        kwargs: dict = {
            "headers": headers,
            "content": message.encode(),
            "timeout": self._timeout,
        }
        if self._auth:
            kwargs["auth"] = self._auth

        async with httpx.AsyncClient() as client:
            resp = await client.post(self._url, **kwargs)

        if resp.status_code != 200:
            raise RuntimeError(
                f"ntfy returned {resp.status_code}: {resp.text[:200]}"
            )


# ------------------------------------------------------------------ #
# Payload builders                                                     #
# ------------------------------------------------------------------ #

def _build_title(p: NotificationPayload) -> str:
    prefix = "🚨 SLASHING RISK" if p.is_slashing_risk else p.alert_type.upper().replace("_", " ")
    return f"{prefix} — {p.host}"


def _build_message(p: NotificationPayload) -> str:
    """
    ntfy message body. Keep it short — this is a phone notification,
    not a rich embed. Full context is in Discord.
    """
    lines = [p.title]

    # Key diagnostics only (first 3 max)
    if p.diagnostics:
        for label, value in list(p.diagnostics.items())[:3]:
            lines.append(f"{label}: {value}")

    if p.is_slashing_risk:
        lines.append("Do NOT restart any validator. Check Discord immediately.")
    elif p.proposed_action:
        lines.append(f"Proposed: {p.proposed_action}")

    lines.append(f"Full context in Discord · incident {p.incident_id}")
    return "\n".join(lines)


def _build_tags(p: NotificationPayload) -> list[str]:
    """
    ntfy tags render as emoji on supported clients.
    https://docs.ntfy.sh/emojis/
    """
    tags = []

    if p.is_slashing_risk:
        tags += ["rotating_light", "no_entry"]
    elif p.severity == Severity.CRITICAL:
        tags += ["rotating_light"]
    elif p.severity == Severity.HIGH:
        tags += ["warning"]
    else:
        tags += ["information_source"]

    tags.append(p.alert_type)   # freeform tag, shown as text label
    return tags
