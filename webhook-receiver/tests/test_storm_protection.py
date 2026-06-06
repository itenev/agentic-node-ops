"""Tests for alert storm protection — bundling and cross-host correlation."""

from datetime import timedelta


from webhook_receiver.storm_protection import (
    StormTracker,
    AlertBundle,
    SINGLE_HOST_THRESHOLD,
)
from webhook_receiver.types import (
    HermesAlert,
    Severity,
    AlertStatus,
    _generate_event_id,
)


def _make_alert(
    alert_type="consensus_desync",
    severity=Severity.HIGH,
    host="validator-01",
) -> HermesAlert:
    return HermesAlert(
        id=_generate_event_id(alert_type),
        alert_type=alert_type,
        severity=severity,
        status=AlertStatus.FIRING,
        host=host,
    )


class TestSingleHostStorm:
    """Test single-host alert bundling (>3 alerts per host in 30s)."""

    def test_no_bundle_below_threshold(self):
        tracker = StormTracker()
        for _ in range(SINGLE_HOST_THRESHOLD):
            alert = _make_alert()
            assert tracker.check_alert(alert) is None

    def test_bundle_exceeds_threshold(self):
        tracker = StormTracker()
        # Send threshold + 1 alerts
        for i in range(SINGLE_HOST_THRESHOLD + 1):
            alert = _make_alert()
            bundle = tracker.check_alert(alert)

        # The last alert should trigger a bundle
        assert bundle is not None
        assert bundle.is_single_host()
        assert bundle.host == "validator-01"
        assert len(bundle.alerts) > SINGLE_HOST_THRESHOLD

    def test_bundle_alert_type_is_storm_single_host(self):
        tracker = StormTracker()
        for i in range(SINGLE_HOST_THRESHOLD + 1):
            alert = _make_alert()
            bundle = tracker.check_alert(alert)

        assert bundle is not None
        storm_alert = bundle.to_alert()
        assert storm_alert.alert_type == "storm_single_host"
        assert "bundle" in storm_alert.raw_labels.get("bundle_id", "")

    def test_bundle_severity_inherits_critical(self):
        """If any bundled alert is critical, the storm alert should be critical."""
        tracker = StormTracker()
        for i in range(SINGLE_HOST_THRESHOLD):
            tracker.check_alert(_make_alert(severity=Severity.MEDIUM))
        bundle = tracker.check_alert(_make_alert(severity=Severity.CRITICAL))

        assert bundle is not None
        storm_alert = bundle.to_alert()
        assert storm_alert.severity == Severity.CRITICAL

    def test_bundle_severity_high_for_non_critical(self):
        tracker = StormTracker()
        for i in range(SINGLE_HOST_THRESHOLD + 1):
            bundle = tracker.check_alert(_make_alert(severity=Severity.HIGH))

        assert bundle is not None
        storm_alert = bundle.to_alert()
        assert storm_alert.severity == Severity.HIGH

    def test_storm_resets_host_counter(self):
        """After a bundle is created, the host counter should reset."""
        tracker = StormTracker()
        # First storm
        for i in range(SINGLE_HOST_THRESHOLD + 1):
            tracker.check_alert(_make_alert())

        # Second storm — should trigger again
        for i in range(SINGLE_HOST_THRESHOLD + 1):
            bundle = tracker.check_alert(_make_alert())

        assert bundle is not None

    def test_expired_alerts_dont_count(self):
        """Alerts outside the 30s window should not contribute to storm."""
        tracker = StormTracker()
        # Manually add old entries
        now = tracker._now()
        old_time = now - timedelta(seconds=60)
        tracker._host_alerts["validator-01"].extend(
            [
                (old_time, "consensus_desync", f"old-{i}")
                for i in range(SINGLE_HOST_THRESHOLD + 1)
            ]
        )

        # New alert should NOT trigger a bundle (old ones expired)
        alert = _make_alert()
        bundle = tracker.check_alert(alert)
        assert bundle is None


class TestCrossHostStorm:
    """Test cross-host correlation (same alert type across >=2 hosts in 60s)."""

    def test_no_bundle_below_threshold(self):
        tracker = StormTracker()
        # Only one host
        alert = _make_alert(host="validator-01")
        assert tracker.check_alert(alert) is None

    def test_bundle_cross_hosts(self):
        tracker = StormTracker()
        # Same alert type on different hosts
        bundle = None
        for host in ["validator-01", "validator-02"]:
            alert = _make_alert(host=host)
            bundle = tracker.check_alert(alert)

        assert bundle is not None
        assert not bundle.is_single_host()
        assert bundle.alert_type == "consensus_desync"
        assert bundle.host is None

    def test_bundle_alert_type_is_storm_cross_host(self):
        tracker = StormTracker()
        for host in ["validator-01", "validator-02"]:
            alert = _make_alert(host=host)
            bundle = tracker.check_alert(alert)

        assert bundle is not None
        storm_alert = bundle.to_alert()
        assert storm_alert.alert_type == "storm_cross_host"
        assert storm_alert.severity == Severity.CRITICAL

    def test_bundle_contains_host_info(self):
        tracker = StormTracker()
        for host in ["validator-01", "validator-02"]:
            alert = _make_alert(host=host)
            bundle = tracker.check_alert(alert)

        storm_alert = bundle.to_alert()
        note = storm_alert.context_snapshot.container_status_note or ""
        assert "cluster-wide" in note.lower() or "2 hosts" in note

    def test_different_alert_types_dont_bundle(self):
        """Different alert types on different hosts should not trigger cross-host storm."""
        tracker = StormTracker()
        tracker.check_alert(
            _make_alert(alert_type="consensus_desync", host="validator-01")
        )
        bundle = tracker.check_alert(
            _make_alert(alert_type="validator_duty_miss", host="validator-02")
        )
        assert bundle is None

    def test_expired_cross_host_alerts_dont_count(self):
        tracker = StormTracker()
        now = tracker._now()
        old_time = now - timedelta(seconds=120)
        tracker._cross_host["consensus_desync"]["validator-01"] = old_time

        # New alert on different host should NOT trigger (old one expired)
        alert = _make_alert(host="validator-02")
        bundle = tracker.check_alert(alert)
        assert bundle is None

    def test_storm_resets_cross_host_counter(self):
        """After a bundle is created, cross-host counter should reset."""
        tracker = StormTracker()
        # First storm
        for host in ["validator-01", "validator-02"]:
            tracker.check_alert(_make_alert(host=host))

        # Second storm — should trigger again
        bundle = None
        for host in ["validator-01", "validator-02"]:
            bundle = tracker.check_alert(_make_alert(host=host))

        assert bundle is not None


class TestAlertBundle:
    """Test AlertBundle data class."""

    def test_single_host_bundle(self):
        alerts = [_make_alert()]
        bundle = AlertBundle(
            bundle_id="test-1",
            alerts=alerts,
            host="validator-01",
            alert_type="mixed",
        )
        assert bundle.is_single_host()
        assert bundle.host == "validator-01"

    def test_cross_host_bundle(self):
        alerts = [_make_alert(), _make_alert(host="validator-02")]
        bundle = AlertBundle(
            bundle_id="test-2",
            alerts=alerts,
            host=None,
            alert_type="consensus_desync",
        )
        assert not bundle.is_single_host()
        assert bundle.host is None

    def test_to_alert_preserves_alert_count(self):
        alerts = [_make_alert() for _ in range(5)]
        bundle = AlertBundle(
            bundle_id="test-3",
            alerts=alerts,
            host="validator-01",
            alert_type="mixed",
        )
        storm_alert = bundle.to_alert()
        assert storm_alert.raw_labels.get("alert_count") == "5"
