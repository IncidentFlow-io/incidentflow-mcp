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
    Severity.WARNING,
    Severity.LOW,
    Severity.INFO,
]

_STRONG_RELATION_THRESHOLD = 0.25


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

    pair_evidence: dict[frozenset[str], list[str]] = {}

    # Two alerts are related only when they have strong shared evidence inside
    # the time window. Time/cluster/env proximity is intentionally weak.
    for a, b in combinations(firing, 2):
        time_diff = abs((a.fired_at - b.fired_at).total_seconds())
        if time_diff > window.total_seconds():
            continue

        score, evidence = _relation_score(a, b)
        if score >= _STRONG_RELATION_THRESHOLD:
            pair_evidence[frozenset({a.alert_id, b.alert_id})] = evidence
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
        evidence = _cluster_evidence(members, pair_evidence)
        confidence = _confidence(members, window, evidence)
        likely_cause = _infer_root_cause(members)
        human_context = _cluster_human_context(members)
        if human_context:
            confidence = min(1.0, round(confidence + 0.1, 2))
            evidence.append("human thread context")
        if confidence < 0.65 and len({alert.service for alert in members}) > 1:
            likely_cause = (
                f"Possible related symptoms across {', '.join(services)} "
                "— missing dependency evidence"
            )

        clusters.append(
            AlertCluster(
                cluster_id=cluster_id,
                alert_ids=sorted(a.alert_id for a in members),
                services=services,
                dominant_severity=dominant,
                likely_root_cause=likely_cause,
                confidence=confidence,
                confidence_level=_confidence_level(confidence),
                evidence=sorted(set(evidence)),
                missing_evidence=_missing_evidence(members, evidence),
                human_context=human_context or None,
            )
        )
        clustered_ids.update(a.alert_id for a in members)

    # Alerts not placed in any qualifying cluster
    uncorrelated = [a.alert_id for a in firing if a.alert_id not in clustered_ids]
    # Include resolved/pending alerts as uncorrelated (they weren't considered)
    uncorrelated += [a.alert_id for a in input_data.alerts if a.status != AlertStatus.FIRING]

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


def _relation_score(a: Alert, b: Alert) -> tuple[float, list[str]]:
    score = 0.03
    evidence = ["within time window"]

    if a.service == b.service:
        score += 0.40
        evidence.append("same service")

    for key, label in (
        ("deployment", "same deployment"),
        ("workload", "same workload"),
        ("pod", "same pod"),
    ):
        if _same_label(a, b, key):
            score += 0.25
            evidence.append(label)
            break

    shared_thread_hints = _thread_hints(a) & _thread_hints(b)
    if shared_thread_hints:
        score += 0.20
        evidence.append("shared human/thread context")

    if _same_label(a, b, "namespace"):
        score += 0.08
        evidence.append("same namespace")

    if _same_label(a, b, "cluster") or _same_label(a, b, "environment") or _same_label(a, b, "env"):
        score += 0.04
        evidence.append("same cluster/environment")

    return min(score, 1.0), evidence


def _same_label(a: Alert, b: Alert, key: str) -> bool:
    left = a.labels.get(key)
    right = b.labels.get(key)
    return bool(left and right and left == right)


def _cluster_evidence(
    members: list[Alert], pair_evidence: dict[frozenset[str], list[str]]
) -> list[str]:
    evidence: list[str] = []
    for a, b in combinations(members, 2):
        evidence.extend(pair_evidence.get(frozenset({a.alert_id, b.alert_id}), []))
    return evidence or ["singleton cluster"]


def _confidence(members: list[Alert], window: timedelta, evidence: list[str]) -> float:
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

    evidence_factor = 0.0
    if "same service" in evidence:
        evidence_factor += 0.35
    if any(item in evidence for item in ("same deployment", "same workload", "same pod")):
        evidence_factor += 0.40
    if "shared human/thread context" in evidence:
        evidence_factor += 0.20
    if "same namespace" in evidence:
        evidence_factor += 0.08
    if "same cluster/environment" in evidence:
        evidence_factor += 0.04

    return round(min(evidence_factor + tightness * 0.05 + size_factor * 0.07, 1.0), 2)


def _confidence_level(confidence: float) -> str:
    if confidence >= 0.85:
        return "strong"
    if confidence >= 0.65:
        return "probable"
    if confidence >= 0.45:
        return "possible"
    return "weak"


def _missing_evidence(members: list[Alert], evidence: list[str]) -> list[str]:
    if len({alert.service for alert in members}) <= 1:
        return []
    missing: list[str] = []
    if "shared human/thread context" not in evidence:
        missing.append("dependency, topology, trace, or human context linking services")
    if not any(item in evidence for item in ("same deployment", "same workload", "same pod")):
        missing.append("shared workload or deployment evidence")
    return missing


def _infer_root_cause(members: list[Alert]) -> str:
    """Best-effort root-cause label from shared alert names and labels."""
    names = [a.name for a in members]
    services = list({a.service for a in members})
    hypotheses = [
        item
        for alert in members
        for item in _thread_analysis(alert).get("engineer_hypotheses", [])
        if isinstance(item, str)
    ]
    if hypotheses:
        return f"Engineer hypothesis: {hypotheses[-1]}"

    # Look for common keywords in alert names
    for keyword in ("database", "db", "memory", "cpu", "disk", "network", "timeout", "latency"):
        if any(keyword in n.lower() for n in names):
            return f"Possible {keyword} issue affecting {', '.join(services)}"

    return f"Correlated alerts across {', '.join(services)} — manual investigation recommended"


def _thread_hints(alert: Alert) -> set[str]:
    analysis = _thread_analysis(alert)
    hints = set()
    for item in analysis.get("mentioned_services", []):
        if isinstance(item, str) and item.strip():
            hints.add(item.strip().lower())
    return hints


def _thread_analysis(alert: Alert) -> dict:
    if not isinstance(alert.thread, dict):
        return {}
    analysis = alert.thread.get("analysis")
    return analysis if isinstance(analysis, dict) else {}


def _cluster_human_context(members: list[Alert]) -> dict[str, object]:
    hypotheses: list[str] = []
    commands: list[str] = []
    runbooks: list[object] = []
    resolution_signal = False
    resolution_confidence = "low"

    for alert in members:
        analysis = _thread_analysis(alert)
        hypotheses.extend(
            item for item in analysis.get("engineer_hypotheses", []) if isinstance(item, str)
        )
        commands.extend(
            item for item in analysis.get("commands_found", []) if isinstance(item, str)
        )
        links = analysis.get("runbook_links", [])
        if isinstance(links, list):
            runbooks.extend(links)
        if analysis.get("resolution_signal"):
            resolution_signal = True
            if analysis.get("resolution_confidence") in {"medium", "high"}:
                resolution_confidence = str(analysis["resolution_confidence"])

    context: dict[str, object] = {}
    if hypotheses:
        context["engineer_hypotheses"] = sorted(set(hypotheses))
    if commands:
        context["commands_found"] = sorted(set(commands))
    if runbooks:
        context["runbook_links"] = runbooks
    if resolution_signal:
        context["resolution_signal"] = True
        context["resolution_confidence"] = resolution_confidence
    return context


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
