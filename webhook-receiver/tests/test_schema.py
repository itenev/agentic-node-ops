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
        payload = _am_payload(alerts=[_am_alert(extra_labels={"client": "lighthouse"})])
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


class TestContainerLabelValidation:
    """Container labels are attacker-controlled; they must be validated at
    the schema boundary before reaching the Docker API request line."""

    def test_valid_container_names_pass(self):
        for name in (
            "consensus",
            "eth-staker-consensus-1",
            "validator.lighthouse",
            "geth_1",
        ):
            payload = _am_payload(alerts=[_am_alert(extra_labels={"container": name})])
            alerts = validate_alertmanager_payload(payload)
            assert alerts[0].container == name

    def test_crlf_injection_label_rejected(self):
        payload = _am_payload(
            alerts=[
                _am_alert(
                    extra_labels={
                        "container": "x HTTP/1.1\r\nGET /version HTTP/1.1\r\n"
                    }
                )
            ]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_space_in_container_label_rejected(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"container": "bad name"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_path_traversal_container_label_rejected(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"container": "../var/run/docker"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_query_string_container_label_rejected(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"container": "x?all=1&t=1"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_unicode_container_label_rejected(self):
        payload = _am_payload(alerts=[_am_alert(extra_labels={"container": "容器"})])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_leading_dash_container_label_rejected(self):
        # first character must be alphanumeric
        payload = _am_payload(alerts=[_am_alert(extra_labels={"container": "-dash"})])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_empty_container_label_ignored(self):
        payload = _am_payload(alerts=[_am_alert(extra_labels={"container": ""})])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_service_label_also_validated(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"service": "bad service"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_trailing_newline_rejected(self):
        """Python's `$` would match before a trailing \\n — the check must
        use fullmatch so "foo\\n" cannot pass as a container name."""
        payload = _am_payload(alerts=[_am_alert(extra_labels={"container": "foo\n"})])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_overlong_name_rejected(self):
        payload = _am_payload(alerts=[_am_alert(extra_labels={"container": "a" * 256})])
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_non_string_label_rejected_not_crash(self):
        """Truthy non-string labels must degrade to None, never raise."""
        for bad in (123, True, 1.5, ["x"], {"a": 1}, b"x"):
            payload = _am_payload(alerts=[_am_alert(extra_labels={"container": bad})])
            alerts = validate_alertmanager_payload(payload)
            assert alerts[0].container is None

    def test_container_name_key_validated(self):
        payload = _am_payload(
            alerts=[_am_alert(extra_labels={"container_name": "bad name"})]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None

    def test_malformed_container_beats_valid_service(self):
        """A malformed container label wins over a valid service label —
        the payload is suspicious, so no container context is attached."""
        payload = _am_payload(
            alerts=[
                _am_alert(extra_labels={"container": "bad name", "service": "goodsvc"})
            ]
        )
        alerts = validate_alertmanager_payload(payload)
        assert alerts[0].container is None
