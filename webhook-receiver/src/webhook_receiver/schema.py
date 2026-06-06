"""Alertmanager payload validation and normalization to HermesAlert."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from .types import AlertStatus, HermesAlert, Severity, _generate_event_id


class ValidationError(Exception):
    """Raised when an Alertmanager payload fails schema validation."""

    pass


def _parse_severity(label: Optional[str]) -> Severity:
    """Map Alertmanager severity label to our Severity enum."""
    if not label:
        return Severity.MEDIUM
    label = label.lower()
    try:
        return Severity(label)
    except ValueError:
        return Severity.MEDIUM


def _parse_status(status: str) -> AlertStatus:
    if status == "firing":
        return AlertStatus.FIRING
    if status == "resolved":
        return AlertStatus.RESOLVED
    raise ValidationError(f"Unknown alert status: {status}")


def _extract_fired_at(alert: dict) -> Optional[str]:
    """Extract or generate ISO-8601 timestamp from alert data."""
    starts_at = alert.get("startsAt")
    if starts_at:
        return starts_at
    return datetime.now(timezone.utc).isoformat()


def _extract_client(labels: dict) -> Optional[str]:
    """Infer the client type from labels."""
    # Common label patterns in eth-docker setups
    for key in ("client", "client_type", "job"):
        val = labels.get(key, "")
        if val:
            # e.g. "lighthouse", "prysm", "teku", "nimbus", "lodestar"
            name = val.lower().split("/")[0].split("-")[0]
            if name in (
                "lighthouse",
                "prysm",
                "teku",
                "nimbus",
                "lodestar",
                "erigon",
                "geth",
                "besu",
                "nethermind",
                "reth",
            ):
                return name
    return None


def _extract_container(labels: dict) -> Optional[str]:
    """Infer container name from labels."""
    for key in ("container", "container_name", "service"):
        val = labels.get(key)
        if val:
            return val
    return None


def normalize_alertmanager_alert(am_alert: dict) -> HermesAlert:
    """
    Convert a single Alertmanager alert dict to a HermesAlert.

    Expected Alertmanager alert schema:
    {
        "status": "firing" | "resolved",
        "labels": {
            "alertname": "...",
            "severity": "...",
            "host": "...",
            ...
        },
        "annotations": { ... },
        "startsAt": "ISO-8601",
        "endsAt": "ISO-8601",
        "generatorURL": "...",
        "fingerprint": "..."
    }
    """
    labels = am_alert.get("labels", {})
    _ = am_alert.get("annotations", {})  # Reserved for future use
    status = am_alert.get("status")

    if not status:
        raise ValidationError("Alert missing 'status' field")
    if not labels:
        raise ValidationError("Alert missing 'labels' field")

    alert_type = labels.get("alertname")
    if not alert_type:
        raise ValidationError("Alert missing 'alertname' label")

    host = labels.get("host", "unknown")

    return HermesAlert(
        id=_generate_event_id(alert_type, time.time()),
        alert_type=alert_type,
        severity=_parse_severity(labels.get("severity")),
        status=_parse_status(status),
        host=host,
        container=_extract_container(labels),
        client=_extract_client(labels),
        fired_at=_extract_fired_at(am_alert),
        runbook_hint=alert_type,  # default: runbook name matches alert name
        raw_labels=labels,
    )


def validate_alertmanager_payload(body: dict) -> list[HermesAlert]:
    """
    Validate a full Alertmanager webhook POST body and return normalized alerts.

    Raises ValidationError if the payload is malformed.
    Returns a list of HermesAlert (may be empty if all alerts resolved).
    """
    alerts = body.get("alerts")
    if not isinstance(alerts, list):
        raise ValidationError("Payload missing 'alerts' array")

    results: list[HermesAlert] = []
    errors: list[str] = []

    for i, alert in enumerate(alerts):
        try:
            results.append(normalize_alertmanager_alert(alert))
        except ValidationError as e:
            errors.append(f"alert[{i}]: {e}")

    if errors:
        raise ValidationError("; ".join(errors))

    return results
