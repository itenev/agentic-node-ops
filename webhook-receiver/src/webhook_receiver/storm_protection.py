"""Alert storm protection — bundling and cross-host correlation.

Two modes:
1. Single-host storm: >3 alerts for same host within 30s → bundle
2. Cross-host correlation: same alert type across >=2 hosts within 60s
   → treat as cluster-wide incident
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .types import HermesAlert

log = logging.getLogger(__name__)

# Thresholds
SINGLE_HOST_THRESHOLD = 3  # alerts per host
SINGLE_HOST_WINDOW = timedelta(seconds=30)
CROSS_HOST_THRESHOLD = 2  # hosts with same alert type
CROSS_HOST_WINDOW = timedelta(seconds=60)


@dataclass
class AlertBundle:
    """A bundled set of alerts from a storm event."""

    bundle_id: str
    alerts: list[HermesAlert]
    host: Optional[str]  # None = cross-host bundle
    alert_type: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_single_host(self) -> bool:
        return self.host is not None

    def to_alert(self) -> HermesAlert:
        """Convert bundle to a single normalized alert for downstream processing."""
        from .types import _generate_event_id, Severity, AlertStatus, ContextSnapshot

        if self.is_single_host():
            alert_type = "storm_single_host"
            host = self.host or "unknown"
            severity = Severity.CRITICAL if any(
                a.severity.value in ("critical",) for a in self.alerts
            ) else Severity.HIGH
            summary = f"Alert storm: {len(self.alerts)} alerts on {host} within 30s"
        else:
            alert_type = "storm_cross_host"
            hosts = sorted(set(a.host for a in self.alerts))
            host = hosts[0]  # primary host for routing
            severity = Severity.CRITICAL
            summary = f"Cluster-wide {self.alert_type} across {len(hosts)} hosts: {', '.join(hosts)}"

        return HermesAlert(
            id=_generate_event_id(self.alert_type),
            alert_type=alert_type,
            severity=severity,
            status=AlertStatus.FIRING,
            host=host,
            context_snapshot=ContextSnapshot(
                container_status_note=summary,
            ),
            runbook_hint=self.alert_type,
            raw_labels={
                "bundle_id": self.bundle_id,
                "alert_count": str(len(self.alerts)),
                "storm_type": "single_host" if self.is_single_host() else "cross_host",
                "original_alert_type": self.alert_type,
            },
        )


class StormTracker:
    """In-memory tracker for detecting alert storms.

    Thread-safe for single-process use (aiohttp is async, not multi-threaded).
    """

    def __init__(
        self,
        single_host_threshold: int = SINGLE_HOST_THRESHOLD,
        single_host_window: timedelta = SINGLE_HOST_WINDOW,
        cross_host_threshold: int = CROSS_HOST_THRESHOLD,
        cross_host_window: timedelta = CROSS_HOST_WINDOW,
    ) -> None:
        self.single_host_threshold = single_host_threshold
        self.single_host_window = single_host_window
        self.cross_host_threshold = cross_host_threshold
        self.cross_host_window = cross_host_window

        # host -> list of (timestamp, alert_type, alert_id)
        self._host_alerts: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
        # alert_type -> dict of host -> timestamp
        self._cross_host: dict[str, dict[str, datetime]] = defaultdict(dict)
        self._bundle_counter = 0

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _next_bundle_id(self) -> str:
        self._bundle_counter += 1
        return f"bundle_{self._bundle_counter}"

    def check_alert(self, alert: HermesAlert) -> Optional[AlertBundle]:
        """
        Check if an alert should be bundled due to storm conditions.

        Returns None if the alert should proceed normally.
        Returns an AlertBundle if a storm threshold has been crossed.

        When a bundle is returned, the triggering alert is the LAST in the bundle.
        """
        now = self._now()

        # Record this alert
        self._host_alerts[alert.host].append((now, alert.alert_type, alert.id))
        self._cross_host[alert.alert_type][alert.host] = now

        # Clean up expired entries
        self._cleanup(now)

        # Check single-host storm
        bundle = self._check_single_host(alert, now)
        if bundle:
            return bundle

        # Check cross-host storm
        bundle = self._check_cross_host(alert, now)
        if bundle:
            return bundle

        return None

    def _check_single_host(
        self, alert: HermesAlert, now: datetime
    ) -> Optional[AlertBundle]:
        """Check if >threshold alerts hit this host within the window."""
        cutoff = now - self.single_host_window
        recent = [
            (ts, atype, aid)
            for ts, atype, aid in self._host_alerts[alert.host]
            if ts >= cutoff
        ]

        if len(recent) > self.single_host_threshold:
            # Collect the recent alerts into a bundle
            # We only have (ts, type, id) — build minimal alerts for the bundle
            bundled_alerts = [alert]  # The current alert
            for ts, atype, aid in recent[:-1]:
                bundled_alerts.append(
                    HermesAlert(
                        id=aid,
                        alert_type=atype,
                        severity=alert.severity,
                        status=alert.status,
                        host=alert.host,
                        fired_at=ts.isoformat(),
                    )
                )

            bundle = AlertBundle(
                bundle_id=self._next_bundle_id(),
                alerts=bundled_alerts,
                host=alert.host,
                alert_type="mixed",  # Could be multiple alert types
            )
            log.warning(
                "Single-host storm detected: %d alerts on %s within %s",
                len(bundled_alerts),
                alert.host,
                self.single_host_window,
            )
            # Clear the host history to avoid re-bundling
            self._host_alerts[alert.host].clear()
            return bundle

        return None

    def _check_cross_host(
        self, alert: HermesAlert, now: datetime
    ) -> Optional[AlertBundle]:
        """Check if same alert type hit >=threshold hosts within the window."""
        cutoff = now - self.cross_host_window
        active_hosts = {
            host: ts
            for host, ts in self._cross_host[alert.alert_type].items()
            if ts >= cutoff
        }

        if len(active_hosts) >= self.cross_host_threshold:
            # Collect alerts from affected hosts
            # We only have host+timestamp, so build minimal alert representations
            bundled_alerts = [alert]
            for host in active_hosts:
                if host != alert.host:
                    bundled_alerts.append(
                        HermesAlert(
                            id=self._next_bundle_id(),
                            alert_type=alert.alert_type,
                            severity=alert.severity,
                            status=alert.status,
                            host=host,
                            fired_at=active_hosts[host].isoformat(),
                        )
                    )

            bundle = AlertBundle(
                bundle_id=self._next_bundle_id(),
                alerts=bundled_alerts,
                host=None,  # cross-host
                alert_type=alert.alert_type,
            )
            log.warning(
                "Cross-host storm detected: %s across %d hosts within %s",
                alert.alert_type,
                len(active_hosts),
                self.cross_host_window,
            )
            # Clear to avoid re-bundling
            self._cross_host[alert.alert_type].clear()
            return bundle

        return None

    def _cleanup(self, now: datetime) -> None:
        """Remove expired entries from tracking structures."""
        single_cutoff = now - self.single_host_window
        cross_cutoff = now - self.cross_host_window

        # Clean host alerts
        for host in list(self._host_alerts.keys()):
            self._host_alerts[host] = [
                (ts, atype, aid)
                for ts, atype, aid in self._host_alerts[host]
                if ts >= single_cutoff
            ]
            if not self._host_alerts[host]:
                del self._host_alerts[host]

        # Clean cross-host tracking
        for atype in list(self._cross_host.keys()):
            self._cross_host[atype] = {
                host: ts
                for host, ts in self._cross_host[atype].items()
                if ts >= cross_cutoff
            }
            if not self._cross_host[atype]:
                del self._cross_host[atype]
