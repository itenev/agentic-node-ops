"""HermesAlert data model and severity enum."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertStatus(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"


SEVERITY_ORDER = {
    Severity.CRITICAL: 1,
    Severity.HIGH: 2,
    Severity.MEDIUM: 3,
    Severity.LOW: 4,
}


@dataclass
class ContextSnapshot:
    head_slot_distance: Optional[int] = None
    peer_count: Optional[int] = None
    container_status: Optional[str] = None
    container_status_note: Optional[str] = None
    validator_count: Optional[int] = None
    prometheus_fallback_used: bool = False

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in {
                "head_slot_distance": self.head_slot_distance,
                "peer_count": self.peer_count,
                "container_status": self.container_status,
                "container_status_note": self.container_status_note,
                "validator_count": self.validator_count,
                "prometheus_fallback_used": self.prometheus_fallback_used,
            }.items()
            if v is not None
        }


@dataclass
class HermesAlert:
    id: str
    alert_type: str
    severity: Severity
    status: AlertStatus
    host: str
    container: Optional[str] = None
    client: Optional[str] = None
    fired_at: Optional[str] = None  # ISO-8601
    context_snapshot: ContextSnapshot = field(default_factory=ContextSnapshot)
    runbook_hint: Optional[str] = None
    raw_labels: dict = field(default_factory=dict)

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "id": self.id,
                "alert_type": self.alert_type,
                "severity": self.severity.value,
                "status": self.status.value,
                "host": self.host,
                "container": self.container,
                "client": self.client,
                "fired_at": self.fired_at,
                "context_snapshot": self.context_snapshot.to_dict(),
                "runbook_hint": self.runbook_hint,
                "raw_labels": self.raw_labels,
            }
        )

    @classmethod
    def from_json(cls, line: str) -> HermesAlert:
        import json

        data = json.loads(line)
        return cls(
            id=data["id"],
            alert_type=data["alert_type"],
            severity=Severity(data["severity"]),
            status=AlertStatus(data["status"]),
            host=data["host"],
            container=data.get("container"),
            client=data.get("client"),
            fired_at=data.get("fired_at"),
            context_snapshot=ContextSnapshot(**data.get("context_snapshot", {})),
            runbook_hint=data.get("runbook_hint"),
            raw_labels=data.get("raw_labels", {}),
        )


def _generate_event_id(alert_type: str, ts: Optional[float] = None) -> str:
    """Generate a short unique event ID: evt_<hash>_<epoch>."""
    ts_int = int(ts or time.time())
    h = hash(f"{alert_type}-{ts_int}") & 0xFFFFFF
    return f"evt_{h:06x}_{ts_int}"
