"""
MCP tool: correlate_alerts

Groups a list of incoming alerts into clusters based on shared service,
severity proximity, and time window.

This implementation uses a straightforward grouping heuristic suitable
for local demo and testing. A production implementation would call an
ML-based correlation engine or AIOps platform.
"""

import hashlib
import logging
from collections import defaultdict
from datetime import timedelta
from itertools import combinations

from incidentflow_mcp.config import get_settings
from incidentflow_mcp.tools.schemas import (
    Alert,
    AlertCluster,
    AlertStatus,
    CorrelateAlertsInput,
    CorrelateAlertsOutput,
    Severity,
)

logger = logging.getLogger(__name__)

# Severity ordering — lower index = more severe
_SEVERITY_ORDER: list[Severity] = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
]


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def correlate_alerts(input_data: CorrelateAlertsInput) -> CorrelateAlertsOutput:
    """
    Cluster the provided alerts by service affinity and time proximity.

    Production integration point: replace the heuristic grouping below
    with a call to your AIOps / correlation backend.
    """
    logger.info("tool:correlate_alerts total_alerts=%d", len(input_data.alerts))

    window = timedelta(minutes=input_data.window_minutes)
    firing = [a for a in input_data.alerts if a.status == AlertStatus.FIRING]

    # Union-Find to cluster alerts
    parent: dict[str, str] = {a.alert_id: a.alert_id for a in firing}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    # Two alerts are related if they share a service OR share a label value
    # and fired within the correlation window
    for a, b in combinations(firing, 2):
        time_diff = abs((a.fired_at - b.fired_at).total_seconds())
        if time_diff > window.total_seconds():
            continue

        shares_service = a.service == b.service
        shared_label_values = set(a.labels.values()) & set(b.labels.values())
        if shares_service or shared_label_values:
            union(a.alert_id, b.alert_id)

    # Group by cluster root
    clusters_map: dict[str, list[Alert]] = defaultdict(list)
    for a in firing:
        clusters_map[find(a.alert_id)].append(a)

    # Build output clusters, filtering by min_cluster_size
    clusters: list[AlertCluster] = []
    clustered_ids: set[str] = set()

    for root, members in clusters_map.items():
        if len(members) < input_data.min_cluster_size:
            continue

        cluster_id = _short_hash(root)
        services = sorted({a.service for a in members})
        dominant = _dominant_severity([a.severity for a in members])
        confidence = _confidence(members, window)
        likely_cause = _infer_root_cause(members)

        clusters.append(
            AlertCluster(
                cluster_id=cluster_id,
                alert_ids=sorted(a.alert_id for a in members),
                services=services,
                dominant_severity=dominant,
                likely_root_cause=likely_cause,
                confidence=confidence,
            )
        )
        clustered_ids.update(a.alert_id for a in members)

    # Alerts not placed in any qualifying cluster
    uncorrelated = [
        a.alert_id for a in firing if a.alert_id not in clustered_ids
    ]
    # Include resolved/pending alerts as uncorrelated (they weren't considered)
    uncorrelated += [
        a.alert_id for a in input_data.alerts if a.status != AlertStatus.FIRING
    ]

    summary = _build_summary(len(input_data.alerts), clusters, uncorrelated)

    return CorrelateAlertsOutput(
        total_alerts=len(input_data.alerts),
        clusters=sorted(clusters, key=lambda c: _SEVERITY_ORDER.index(c.dominant_severity)),
        uncorrelated_alert_ids=sorted(uncorrelated),
        analysis_window_minutes=input_data.window_minutes,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _dominant_severity(severities: list[Severity]) -> Severity:
    """Return the most severe value from a list."""
    for sev in _SEVERITY_ORDER:
        if sev in severities:
            return sev
    return Severity.INFO


def _confidence(members: list[Alert], window: timedelta) -> float:
    """
    Heuristic confidence score [0.0, 1.0] based on cluster size and
    how tightly alerts are grouped within the time window.
    """
    if len(members) == 1:
        return 0.5

    timestamps = sorted(a.fired_at for a in members)
    span = (timestamps[-1] - timestamps[0]).total_seconds()
    tightness = 1.0 - min(span / window.total_seconds(), 1.0)
    size_factor = min(len(members) / 10.0, 1.0)

    return round((tightness * 0.6 + size_factor * 0.4), 2)


def _infer_root_cause(members: list[Alert]) -> str:
    """Best-effort root-cause label from shared alert names and labels."""
    names = [a.name for a in members]
    services = list({a.service for a in members})

    # Look for common keywords in alert names
    for keyword in ("database", "db", "memory", "cpu", "disk", "network", "timeout", "latency"):
        if any(keyword in n.lower() for n in names):
            return f"Possible {keyword} issue affecting {', '.join(services)}"

    return f"Correlated alerts across {', '.join(services)} — manual investigation recommended"


def _build_summary(total: int, clusters: list[AlertCluster], uncorrelated: list[str]) -> str:
    if not clusters:
        return (
            f"No correlated clusters found among {total} alert(s). "
            f"{len(uncorrelated)} alert(s) remain uncorrelated."
        )
    critical = sum(1 for c in clusters if c.dominant_severity == Severity.CRITICAL)
    return (
        f"Found {len(clusters)} cluster(s) from {total} alert(s). "
        f"{critical} cluster(s) are CRITICAL severity. "
        f"{len(uncorrelated)} alert(s) uncorrelated."
    )


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:8]
