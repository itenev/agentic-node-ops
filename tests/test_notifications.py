"""
tests/test_notifications.py

Unit tests — no real HTTP calls, all transports are mocked.

Run:
    pytest tests/ -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from agentic_node_ops.types import (
    NotificationPayload,
    NotificationResult,
    Severity,
    CHANNEL_ROUTING,
    NTFY_PRIORITY,
    DISCORD_COLOR,
)
from agentic_node_ops.discord import DiscordNotifier, _truncate
from agentic_node_ops.ntfy import _build_title, _build_message, _build_tags
from agentic_node_ops.dispatcher import NotificationDispatcher


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


def make_payload(
    severity=Severity.HIGH,
    alert_type="consensus_desync",
    is_slashing=False,
    approval_required=False,
) -> NotificationPayload:
    return NotificationPayload(
        incident_id="evt_abc123_1704067200",
        alert_type=alert_type,
        severity=severity,
        host="validator-01",
        title="Lighthouse is 184 slots behind",
        summary="Peer count collapsed from 68 → 2. Likely networking issue.",
        diagnostics={"Peer count": "2", "Slot distance": "184", "EL status": "synced"},
        runbook_id="consensus_desync",
        proposed_action="Restart lighthouse container" if approval_required else None,
        approval_required=approval_required,
        is_slashing_risk=is_slashing,
        forensic_path="/var/hermes/incidents/slash_20250101/" if is_slashing else None,
    )


# ------------------------------------------------------------------ #
# Routing table                                                        #
# ------------------------------------------------------------------ #


class TestRoutingTable:
    def test_low_medium_high_discord_only(self):
        for sev in (Severity.LOW, Severity.MEDIUM, Severity.HIGH):
            assert CHANNEL_ROUTING[sev] == ["discord"]

    def test_critical_both_channels(self):
        assert set(CHANNEL_ROUTING[Severity.CRITICAL]) == {"discord", "ntfy"}

    def test_ntfy_priority_urgent_for_critical(self):
        assert NTFY_PRIORITY[Severity.CRITICAL] == "urgent"

    def test_ntfy_priority_high_for_high(self):
        assert NTFY_PRIORITY[Severity.HIGH] == "high"


# ------------------------------------------------------------------ #
# Discord embed construction                                           #
# ------------------------------------------------------------------ #


class TestDiscordEmbed:
    def setup_method(self):
        self.notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/fake/url"
        )

    def test_embed_color_matches_severity(self):
        for sev in Severity:
            p = make_payload(severity=sev)
            embed = self.notifier._build_embed(p)
            assert embed["color"] == DISCORD_COLOR[sev]

    def test_embed_title_contains_alert_type_phrase(self):
        p = make_payload(severity=Severity.HIGH)
        embed = self.notifier._build_embed(p)
        assert "Lighthouse is 184 slots behind" in embed["title"]

    def test_embed_footer_contains_incident_id(self):
        p = make_payload()
        embed = self.notifier._build_embed(p)
        assert "evt_abc123_1704067200" in embed["footer"]["text"]

    def test_embed_diagnostic_fields_inline(self):
        p = make_payload()
        embed = self.notifier._build_embed(p)
        field_names = [f["name"] for f in embed["fields"]]
        assert "Peer count" in field_names
        assert "Slot distance" in field_names

    def test_proposed_action_field_appears_when_set(self):
        p = make_payload(approval_required=True)
        embed = self.notifier._build_embed(p)
        field_names = [f["name"] for f in embed["fields"]]
        assert "Proposed action" in field_names

    def test_no_proposed_action_field_when_absent(self):
        p = make_payload(approval_required=False)
        embed = self.notifier._build_embed(p)
        field_names = [f["name"] for f in embed["fields"]]
        assert "Proposed action" not in field_names

    def test_slashing_forensic_field_present(self):
        p = make_payload(is_slashing=True, severity=Severity.CRITICAL)
        embed = self.notifier._build_embed(p)
        field_names = [f["name"] for f in embed["fields"]]
        assert any("Evidence" in n for n in field_names)

    def test_at_here_on_critical(self):
        p = make_payload(severity=Severity.CRITICAL)
        content = self.notifier._build_content(p)
        assert content == "@here"

    def test_no_at_here_on_high(self):
        p = make_payload(severity=Severity.HIGH)
        content = self.notifier._build_content(p)
        assert content is None

    def test_at_here_on_slashing_regardless_of_severity(self):
        p = make_payload(is_slashing=True, severity=Severity.MEDIUM)
        content = self.notifier._build_content(p)
        assert content == "@here"

    def test_truncate_helper(self):
        assert _truncate("hello", 10) == "hello"
        assert len(_truncate("x" * 300, 256)) == 256
        assert _truncate("x" * 300, 256).endswith("...")


# ------------------------------------------------------------------ #
# ntfy payload construction                                            #
# ------------------------------------------------------------------ #


class TestNtfyPayload:
    def test_slashing_title_prefix(self):
        p = make_payload(is_slashing=True, severity=Severity.CRITICAL)
        assert _build_title(p).startswith("🚨 SLASHING RISK")

    def test_normal_title_uses_alert_type(self):
        p = make_payload(alert_type="client_crash", severity=Severity.HIGH)
        assert "CLIENT CRASH" in _build_title(p)

    def test_message_includes_diagnostics(self):
        p = make_payload()
        msg = _build_message(p)
        assert "Peer count" in msg

    def test_message_max_3_diagnostics(self):
        p = make_payload()
        p.diagnostics = {f"key_{i}": f"val_{i}" for i in range(10)}
        msg = _build_message(p)
        # Only first 3 labels should appear
        assert "key_3" not in msg

    def test_slashing_message_contains_warning(self):
        p = make_payload(is_slashing=True, severity=Severity.CRITICAL)
        msg = _build_message(p)
        assert "Do NOT restart" in msg

    def test_rotating_light_tag_on_critical(self):
        p = make_payload(severity=Severity.CRITICAL)
        tags = _build_tags(p)
        assert "rotating_light" in tags

    def test_slashing_tags_include_no_entry(self):
        p = make_payload(is_slashing=True, severity=Severity.CRITICAL)
        tags = _build_tags(p)
        assert "no_entry" in tags

    def test_alert_type_always_in_tags(self):
        p = make_payload(alert_type="validator_duty_misses")
        tags = _build_tags(p)
        assert "validator_duty_misses" in tags


# ------------------------------------------------------------------ #
# Dispatcher routing                                                   #
# ------------------------------------------------------------------ #


class TestDispatcher:
    def _make_dispatcher(self, discord_result=None, ntfy_result=None):
        d = NotificationDispatcher.__new__(NotificationDispatcher)
        d._discord = MagicMock()
        d._ntfy = MagicMock()
        d._discord.send = AsyncMock(
            return_value=discord_result or NotificationResult("discord", True, "msg123")
        )
        d._ntfy.send = AsyncMock(
            return_value=ntfy_result or NotificationResult("ntfy", True)
        )
        return d

    async def test_high_severity_discord_only(self):
        dispatcher = self._make_dispatcher()
        p = make_payload(severity=Severity.HIGH)
        await dispatcher.dispatch(p)
        dispatcher._discord.send.assert_called_once()
        dispatcher._ntfy.send.assert_not_called()

    async def test_critical_severity_both_channels(self):
        dispatcher = self._make_dispatcher()
        p = make_payload(severity=Severity.CRITICAL)
        await dispatcher.dispatch(p)
        dispatcher._discord.send.assert_called_once()
        dispatcher._ntfy.send.assert_called_once()

    async def test_slashing_always_fires_ntfy(self):
        dispatcher = self._make_dispatcher()
        # High severity normally skips ntfy
        p = make_payload(severity=Severity.HIGH, is_slashing=True)
        await dispatcher.dispatch(p)
        dispatcher._ntfy.send.assert_called_once()

    async def test_dispatch_returns_results_list(self):
        dispatcher = self._make_dispatcher()
        p = make_payload(severity=Severity.CRITICAL)
        results = await dispatcher.dispatch(p)
        assert isinstance(results, list)
        assert all(isinstance(r, NotificationResult) for r in results)

    async def test_dispatch_never_raises_on_discord_failure(self):
        dispatcher = self._make_dispatcher(
            discord_result=NotificationResult("discord", False, error="timeout")
        )
        p = make_payload(severity=Severity.HIGH)
        # Should not raise
        results = await dispatcher.dispatch(p)
        assert results[0].success is False
        assert results[0].error == "timeout"

    def test_dispatcher_requires_webhook_url(self):
        with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
            NotificationDispatcher(discord_webhook_url="", ntfy_topic="test-topic")

    def test_dispatcher_requires_ntfy_topic(self):
        with pytest.raises(ValueError, match="NTFY_TOPIC"):
            NotificationDispatcher(
                discord_webhook_url="https://discord.com/fake", ntfy_topic=""
            )
