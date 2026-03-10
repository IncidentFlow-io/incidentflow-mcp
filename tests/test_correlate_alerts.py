"""
Unit tests for the correlate_alerts tool.
"""

from datetime import datetime, timezone

import pytest

from incidentflow_mcp.tools.correlate_alerts import correlate_alerts
from incidentflow_mcp.tools.schemas import (
    Alert,
    AlertStatus,
    CorrelateAlertsInput,
    Severity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _alert(
    alert_id: str,
    service: str,
    severity: Severity = Severity.HIGH,
    status: AlertStatus = AlertStatus.FIRING,
    offset_minutes: int = 0,
    labels: dict[str, str] | None = None,
) -> Alert:
    from datetime import timedelta

    return Alert(
        alert_id=alert_id,
        name=f"alert-{alert_id}",
        service=service,
        severity=severity,
        status=status,
        fired_at=_NOW + timedelta(minutes=offset_minutes),
        labels=labels or {},
    )


# ---------------------------------------------------------------------------
# Basic correlation
# ---------------------------------------------------------------------------


class TestCorrelateAlertsBasic:
    def test_single_service_cluster(self) -> None:
        alerts = [
            _alert("a1", "payments", offset_minutes=0),
            _alert("a2", "payments", offset_minutes=5),
            _alert("a3", "payments", offset_minutes=10),
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert len(result.clusters) == 1
        assert result.clusters[0].alert_ids == ["a1", "a2", "a3"]

    def test_alerts_in_separate_services_not_clustered(self) -> None:
        alerts = [
            _alert("a1", "service-alpha", offset_minutes=0),
            _alert("a2", "service-beta", offset_minutes=5),
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        # No shared service, no shared labels → no cluster of size ≥ 2
        assert len(result.clusters) == 0
        assert set(result.uncorrelated_alert_ids) == {"a1", "a2"}

    def test_total_alerts_count(self) -> None:
        alerts = [_alert(f"a{i}", "svc", offset_minutes=i) for i in range(5)]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert result.total_alerts == 5

    def test_analysis_window_reflected(self) -> None:
        alerts = [_alert("a1", "svc"), _alert("a2", "svc")]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=30, min_cluster_size=2)
        )
        assert result.analysis_window_minutes == 30


class TestCorrelateAlertsTimeWindow:
    def test_alerts_outside_window_not_correlated(self) -> None:
        alerts = [
            _alert("a1", "payments", offset_minutes=0),
            _alert("a2", "payments", offset_minutes=120),  # outside 60-min window
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert len(result.clusters) == 0

    def test_alerts_on_window_boundary_correlated(self) -> None:
        alerts = [
            _alert("a1", "payments", offset_minutes=0),
            _alert("a2", "payments", offset_minutes=59),  # just inside 60-min window
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert len(result.clusters) == 1


class TestCorrelateAlertsLabelAffinity:
    def test_shared_label_correlates_across_services(self) -> None:
        alerts = [
            _alert("a1", "service-x", labels={"env": "prod"}),
            _alert("a2", "service-y", labels={"env": "prod"}),
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert len(result.clusters) == 1


class TestCorrelateAlertsMinClusterSize:
    def test_min_cluster_size_1_includes_singletons(self) -> None:
        alerts = [
            _alert("a1", "payments"),
            _alert("a2", "auth-service"),  # different service, no shared labels
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=1)
        )
        # Each singleton is its own cluster of size 1
        assert len(result.clusters) == 2

    def test_min_cluster_size_filters_small_clusters(self) -> None:
        alerts = [_alert("a1", "payments"), _alert("a2", "auth-service")]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=3)
        )
        assert len(result.clusters) == 0


class TestCorrelateAlertsSeverity:
    def test_dominant_severity_is_most_critical(self) -> None:
        alerts = [
            _alert("a1", "payments", severity=Severity.CRITICAL),
            _alert("a2", "payments", severity=Severity.LOW),
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert result.clusters[0].dominant_severity == Severity.CRITICAL


class TestCorrelateAlertsResolvedAlerts:
    def test_resolved_alerts_go_to_uncorrelated(self) -> None:
        alerts = [
            _alert("a1", "payments", status=AlertStatus.RESOLVED),
            _alert("a2", "payments", status=AlertStatus.FIRING),
        ]
        result = correlate_alerts(
            CorrelateAlertsInput(alerts=alerts, window_minutes=60, min_cluster_size=2)
        )
        assert "a1" in result.uncorrelated_alert_ids


class TestCorrelateAlertsInputValidation:
    def test_no_firing_alerts_raises(self) -> None:
        alerts = [_alert("a1", "svc", status=AlertStatus.RESOLVED)]
        with pytest.raises(Exception):
            CorrelateAlertsInput(alerts=alerts)

    def test_empty_alerts_raises(self) -> None:
        with pytest.raises(Exception):
            CorrelateAlertsInput(alerts=[])

    def test_negative_window_raises(self) -> None:
        alerts = [_alert("a1", "svc")]
        with pytest.raises(Exception):
            CorrelateAlertsInput(alerts=alerts, window_minutes=-1)
