"""Tests for Alertmanager payload validation and normalization."""

import pytest

from webhook_receiver.schema import ValidationError, validate_alertmanager_payload
from webhook_receiver.types import AlertStatus, Severity


def _am_alert(
    alertname="consensus_desync",
    severity="high",
    host="validator-01",
    status="firing",
    extra_labels=None,
) -> dict:
    alert = {
        "status": status,
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "host": host,
        },
        "annotations": {"summary": "Test alert"},
        "startsAt": "2025-01-01T00:00:00Z",
    }
    if extra_labels:
        alert["labels"].update(extra_labels)
    return alert


def _am_payload(alerts=None, status="firing") -> dict:
    if alerts is None:
        alerts = [_am_alert()]
    return {
        "version": "4",
        "status": status,
        "alerts": alerts,
        "commonLabels": {},
        "externalURL": "http://alertmanager:9093",
    }


class TestValidatePayload:
    def test_valid_single_alert(self):
        payload = _am_payload()
        alerts = validate_alertmanager_payload(payload)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "consensus_desync"
        assert alerts[0].severity == Severity.HIGH
        assert alerts[0].status == AlertStatus.FIRING
        assert alerts[0].host == "validator-01"
        assert alerts[0].runbook_hint == "consensus_desync"

    def test_valid_multiple_alerts(self):
        payload = _am_payload(
            alerts=[
                _am_alert("consensus_desync", "critical", "host-1"),
                _am_alert("validator_duty_miss", "high", "host-2"),
            ]
        )
        alerts = validate_alertmanager_payload(payload)
        assert len(alerts) == 2
        assert alerts[0].alert_type == "consensus_desync"
        assert alerts[1].alert_type == "validator_duty_miss"

    def test_resolved_alert(self):
        payload = _am_payload(alerts=[_am_alert(status="resolved")])
        alerts = validate_alertmanager_payload(payload)
        assert len(alerts) == 1
        assert alerts[0].status == AlertStatus.RESOLVED

    def test_missing_alerts_key(self):
        with pytest.raises(ValidationError, match="missing 'alerts' array"):
            validate_alertmanager_payload({"version": "4"})

    def test_alerts_not_a_list(self):
        with pytest.raises(ValidationError, match="missing 'alerts' array"):
            validate_alertmanager_payload({"alerts": "not_a_list"})

    def test_missing_alertname(self):
        payload = _am_payload()
        payload["alerts"][0]["labels"].pop("alertname")
        with pytest.raises(ValidationError, match="missing 'alertname' label"):
            validate_alertmanager_payload(payload)

    def test_missing_status(self):
        payload = _am_payload()
        payload["alerts"][0].pop("status")
        with pytest.raises(ValidationError, match="missing 'status' field"):
            validate_alertmanager_payload(payload)

    def test_unknown_severity_defaults_to_medium(self):
        payload = _am_payload(alerts=[_am_alert(severity="banana")])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].severity == Severity.MEDIUM

    def test_empty_alerts_list(self):
        payload = _am_payload(alerts=[])
        alerts = validate_alertmanager_payload(payload)
        assert len(alerts) == 0

    def test_client_extraction_from_labels(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"client": "lighthouse"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].client == "lighthouse"

    def test_container_extraction_from_labels(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"container": "consensus"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container == "consensus"

    def test_default_host_is_unknown(self):
        payload = _am_payload()
        payload["alerts"][0]["labels"].pop("host")
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].host == "unknown"
