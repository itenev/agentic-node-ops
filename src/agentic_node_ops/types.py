"""
hermes/notifications/types.py

Shared types for the notification layer.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Routing table: which channels fire at each severity.
# Tier 1 = Discord (always).
# Tier 2 = ntfy.sh (critical only, wakes operator through silent mode).
CHANNEL_ROUTING: dict[Severity, list[str]] = {
    Severity.LOW: ["discord"],
    Severity.MEDIUM: ["discord"],
    Severity.HIGH: ["discord"],
    Severity.CRITICAL: ["discord", "ntfy"],
}

# ntfy priority mapping
NTFY_PRIORITY: dict[Severity, str] = {
    Severity.LOW: "low",
    Severity.MEDIUM: "default",
    Severity.HIGH: "high",
    Severity.CRITICAL: "urgent",  # bypasses iOS/Android silent mode
}

# Discord embed color per severity (decimal)
DISCORD_COLOR: dict[Severity, int] = {
    Severity.LOW: 0x888780,  # gray
    Severity.MEDIUM: 0xEF9F27,  # amber
    Severity.HIGH: 0xD85A30,  # coral/orange
    Severity.CRITICAL: 0xE24B4A,  # red
}

SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.LOW: "🔵",
    Severity.MEDIUM: "🟡",
    Severity.HIGH: "🟠",
    Severity.CRITICAL: "🔴",
}


@dataclass
class NotificationPayload:
    """
    Everything the notification layer needs.
    Constructed by Hermes after reasoning; no raw alert structures here.
    """

    incident_id: str
    alert_type: str
    severity: Severity
    host: str
    title: str  # short, operator-facing headline
    summary: str  # Hermes-generated explanation (markdown ok)
    diagnostics: dict[str, str] = field(default_factory=dict)  # label → value
    runbook_id: Optional[str] = None
    proposed_action: Optional[str] = (
        None  # human-readable description if Tier 2 action exists
    )
    approval_required: bool = False
    # Slashing incidents carry extra forensic fields
    is_slashing_risk: bool = False
    forensic_path: Optional[str] = None  # path to on-disk evidence bundle


@dataclass
class NotificationResult:
    channel: str
    success: bool
    message_id: Optional[str] = None  # Discord message ID for future edits
    error: Optional[str] = None
