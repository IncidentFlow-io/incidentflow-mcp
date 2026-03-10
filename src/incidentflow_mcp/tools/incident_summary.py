"""
MCP tool: incident_summary

Returns a structured summary for a given incident ID.

In production this tool would call your incident management backend
(PagerDuty, OpsGenie, Jira, etc.). For local dev/demo it returns
realistic synthetic data so the tool is fully exercisable without
external dependencies.
"""

import logging
from datetime import datetime, timezone

from incidentflow_mcp.tools.schemas import (
    IncidentSummaryInput,
    IncidentSummaryOutput,
    Severity,
    TimelineEvent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthetic data store (replace with real backend calls in production)
# ---------------------------------------------------------------------------

_FAKE_INCIDENTS: dict[str, dict] = {
    "INC-001": {
        "title": "Database connection pool exhausted on payments-service",
        "severity": Severity.CRITICAL,
        "status": "investigating",
        "summary": (
            "The payments-service database connection pool became fully exhausted "
            "at 03:14 UTC causing HTTP 503 responses for all payment endpoints. "
            "Root cause traced to a missing index on the transactions table causing "
            "long-running queries that held connections open."
        ),
        "affected_services": ["payments-service", "checkout-service", "fraud-detection"],
        "timeline": [
            {"timestamp": "2026-01-15T03:14:00Z", "description": "Alert fired: DB pool >90%", "actor": "prometheus"},
            {"timestamp": "2026-01-15T03:15:30Z", "description": "PagerDuty escalation sent", "actor": "pagerduty"},
            {"timestamp": "2026-01-15T03:22:00Z", "description": "On-call engineer acknowledged", "actor": "alice@example.com"},
            {"timestamp": "2026-01-15T03:45:00Z", "description": "Missing index identified in slow query log", "actor": "alice@example.com"},
        ],
        "recommendations": [
            "Add index on transactions(created_at, status) to reduce query latency",
            "Increase connection pool timeout threshold alert to trigger earlier",
            "Add circuit breaker on payments-service → database calls",
            "Review slow query log weekly as part of SRE hygiene",
        ],
    },
    "INC-002": {
        "title": "Memory leak in notification-worker causing OOMKilled pods",
        "severity": Severity.HIGH,
        "status": "mitigated",
        "summary": (
            "notification-worker pods were OOMKilled repeatedly after a deployment "
            "introducing a new email template renderer that accumulated template "
            "objects in memory. Rolling back the deployment resolved the crashes."
        ),
        "affected_services": ["notification-worker", "email-gateway"],
        "timeline": [
            {"timestamp": "2026-02-03T11:00:00Z", "description": "Deployment notification-worker@v2.3.1 rolled out", "actor": "ci/cd"},
            {"timestamp": "2026-02-03T11:45:00Z", "description": "OOMKilled pods detected — restartCount > 3", "actor": "kubernetes"},
            {"timestamp": "2026-02-03T12:00:00Z", "description": "Rollback to v2.3.0 initiated", "actor": "bob@example.com"},
            {"timestamp": "2026-02-03T12:05:00Z", "description": "Pods stable — incident mitigated", "actor": "bob@example.com"},
        ],
        "recommendations": [
            "Profile template renderer for memory leaks before re-deploying v2.3.1",
            "Add memory limit alerts at 80% threshold for notification-worker",
            "Require memory profiling step in CI for services with template rendering",
        ],
    },
}

_UNKNOWN_INCIDENT: dict = {
    "title": "Unknown incident",
    "severity": Severity.INFO,
    "status": "not_found",
    "summary": "No data found for the requested incident ID.",
    "affected_services": [],
    "timeline": [],
    "recommendations": ["Verify the incident ID and try again."],
}


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def incident_summary(input_data: IncidentSummaryInput) -> IncidentSummaryOutput:
    """
    Return a structured summary for the specified incident.

    Production integration point: replace _FAKE_INCIDENTS lookup with
    an async call to your incident management API.
    """
    logger.info("tool:incident_summary incident_id=%s", input_data.incident_id)

    raw = _FAKE_INCIDENTS.get(input_data.incident_id, _UNKNOWN_INCIDENT)

    timeline: list[TimelineEvent] = []
    if input_data.include_timeline:
        for event in raw["timeline"]:
            ts = event["timestamp"]
            dt = (
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if isinstance(ts, str)
                else ts
            )
            timeline.append(
                TimelineEvent(
                    timestamp=dt,
                    description=event["description"],
                    actor=event.get("actor"),
                )
            )

    affected = raw["affected_services"] if input_data.include_affected_services else []

    return IncidentSummaryOutput(
        incident_id=input_data.incident_id,
        title=raw["title"],
        severity=raw["severity"],
        status=raw["status"],
        summary=raw["summary"],
        affected_services=affected,
        timeline=timeline,
        recommendations=raw["recommendations"],
    )
