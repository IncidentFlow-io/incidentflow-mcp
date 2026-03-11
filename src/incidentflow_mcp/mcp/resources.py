"""
MCP resources — read-only incident data exposed to AI agents.

Registered resources:
  incidents://recent              — brief list of recent incidents
  incidents://{incident_id}       — full details for a specific incident
  incidents://timeline/{incident_id} — timeline events for a specific incident

Call register_resources(mcp) from create_mcp_server() to register all resources.

Production integration: replace _FAKE_INCIDENTS lookups with calls to your
real backend (IncidentFlow API, PagerDuty, OpsGenie, etc.).
"""

import logging

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.tools.incident_summary import _FAKE_INCIDENTS

logger = logging.getLogger(__name__)


def register_resources(mcp: FastMCP) -> None:
    """Register all MCP resources on the given FastMCP instance."""

    @mcp.resource(
        "incidents://recent",
        name="recent_incidents",
        description=(
            "Brief list of recent incidents: "
            "id, title, severity, and current status."
        ),
        mime_type="application/json",
    )
    def recent_incidents() -> list[dict]:
        """Return a brief list of all known incidents."""
        logger.debug("resource:incidents://recent — %d records", len(_FAKE_INCIDENTS))
        return [
            {
                "incident_id": incident_id,
                "title": data["title"],
                "severity": str(data["severity"]),
                "status": data["status"],
            }
            for incident_id, data in _FAKE_INCIDENTS.items()
        ]

    @mcp.resource(
        "incidents://{incident_id}",
        name="incident_detail",
        description=(
            "Full incident details: title, severity, status, summary, "
            "affected services, timeline, and recommendations."
        ),
        mime_type="application/json",
    )
    def incident_detail(incident_id: str) -> dict:
        """Return full details for the specified incident."""
        logger.debug("resource:incidents://%s", incident_id)
        data = _FAKE_INCIDENTS.get(incident_id)
        if data is None:
            return {
                "incident_id": incident_id,
                "error": "not_found",
                "detail": f"Incident '{incident_id}' not found.",
            }
        return {
            "incident_id": incident_id,
            "title": data["title"],
            "severity": str(data["severity"]),
            "status": data["status"],
            "summary": data["summary"],
            "affected_services": data["affected_services"],
            "timeline": [
                {
                    "timestamp": str(event["timestamp"]),
                    "description": event["description"],
                    "actor": event.get("actor"),
                }
                for event in data["timeline"]
            ],
            "recommendations": data["recommendations"],
        }

    @mcp.resource(
        "incidents://timeline/{incident_id}",
        name="incident_timeline",
        description=(
            "Incident event timeline: timestamps, step descriptions, "
            "and actors (systems / users)."
        ),
        mime_type="application/json",
    )
    def incident_timeline(incident_id: str) -> dict:
        """Return only the timeline for the specified incident."""
        logger.debug("resource:incidents://timeline/%s", incident_id)
        data = _FAKE_INCIDENTS.get(incident_id)
        if data is None:
            return {
                "incident_id": incident_id,
                "error": "not_found",
                "detail": f"Incident '{incident_id}' not found.",
            }
        return {
            "incident_id": incident_id,
            "timeline": [
                {
                    "timestamp": str(event["timestamp"]),
                    "description": event["description"],
                    "actor": event.get("actor"),
                }
                for event in data["timeline"]
            ],
        }
