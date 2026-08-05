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
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.config import get_settings
from incidentflow_mcp.tools.incident_summary import _FAKE_INCIDENTS

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_MCP_ROOT = _PACKAGE_ROOT.parents[1]
_LOCAL_WIDGET_DIST = _MCP_ROOT / "apps" / "chatgpt-widgets" / "dist" / "index.html"
_PACKAGED_WIDGET_DIST = _PACKAGE_ROOT / "assets" / "grafana-panel-widget" / "index.html"
_WIDGET_FALLBACK = _PACKAGE_ROOT / "assets" / "grafana-panel.html"


def _grafana_widget_meta(grafana_public_base_url: str) -> dict:
    grafana_origin = grafana_public_base_url.rstrip("/")
    resource_domains = ["https://persistent.oaistatic.com", grafana_origin]
    return {
        "ui": {
            "prefersBorder": True,
            "csp": {
                "connectDomains": [],
                "resourceDomains": resource_domains,
            },
        },
        "openai/widgetDescription": (
            "Interactive Grafana panel view with zoom, legend, and interval selection."
        ),
        "openai/widgetPrefersBorder": True,
        "openai/widgetAccessible": True,
        "openai/widgetCSP": {
            "connect_domains": [],
            "resource_domains": resource_domains,
        },
    }


def register_resources(mcp: FastMCP) -> None:
    """Register all MCP resources on the given FastMCP instance."""
    settings = get_settings()

    @mcp.resource(
        "incidents://recent",
        name="recent_incidents",
        description=("Brief list of recent incidents: id, title, severity, and current status."),
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
            "Incident event timeline: timestamps, step descriptions, and actors (systems / users)."
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

    @mcp.resource(
        "ui://incidentflow/grafana-panel.html",
        name="grafana_panel_widget",
        description="Interactive Grafana timeseries panel widget for ChatGPT Apps SDK.",
        mime_type="text/html",
        meta=_grafana_widget_meta(settings.grafana_public_base_url),
    )
    def grafana_panel_widget() -> str:
        if _LOCAL_WIDGET_DIST.exists():
            path = _LOCAL_WIDGET_DIST
        elif _PACKAGED_WIDGET_DIST.exists():
            path = _PACKAGED_WIDGET_DIST
        else:
            path = _WIDGET_FALLBACK
        logger.debug("resource:ui://incidentflow/grafana-panel.html path=%s", path)
        return path.read_text(encoding="utf-8")
