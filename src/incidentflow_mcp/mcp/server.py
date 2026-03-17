"""
MCP server definition.

Uses FastMCP (official MCP Python SDK) with Streamable HTTP transport.
All tools are registered here and wired to their implementation modules.
"""

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.config import get_settings
from incidentflow_mcp.mcp.resources import register_resources
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.tools.correlate_alerts import correlate_alerts as _correlate_alerts_impl
from incidentflow_mcp.tools.incident_summary import incident_summary as _incident_summary_impl
from incidentflow_mcp.tools.registry import get_tool_specs
from incidentflow_mcp.tools.schemas import (
    CorrelateAlertsInput,
    CorrelateAlertsOutput,
    IncidentSummaryInput,
    IncidentSummaryOutput,
)

logger = logging.getLogger(__name__)

_VALID_EXECUTION_MODES = {"auto", "sync", "async"}


def _resolve_execution_mode(settings, requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    if mode == "auto":
        return "async" if settings.async_tools_enabled() else "sync"
    return mode


def _build_async_result(
    *,
    job_id: str,
    status: str,
    poll_after_seconds: int,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "mode": "async",
        "job_id": job_id,
        "status": status,
        "poll_after_seconds": poll_after_seconds,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2)


def create_mcp_server() -> FastMCP:
    """
    Instantiate and configure the FastMCP server with all registered tools.

    Returns a FastMCP instance whose `streamable_http_app()` can be mounted
    into a FastAPI/Starlette application.
    """
    settings = get_settings()

    mcp = FastMCP(
        name=settings.mcp_server_name,
        # host="0.0.0.0" prevents FastMCP from auto-enabling DNS-rebinding
        # protection (which only activates for 127.0.0.1 / localhost).
        # Actual bind address is controlled by uvicorn in the CLI.
        host="0.0.0.0",
        # stateless_http=True handles each request independently — safe for
        # horizontal scaling. Set to False for SSE-based streaming sessions.
        stateless_http=True,
        # streamable_http_path="/mcp": the FastMCP sub-app's internal route
        # lives at "/mcp".  Our FastAPI catch-all at /mcp forwards the full
        # scope (path="/mcp") directly — no prefix stripping — so this matches.
        streamable_http_path="/mcp",
    )

    # ------------------------------------------------------------------
    # Tool: incident_summary
    # ------------------------------------------------------------------
    _specs = {s.name: s for s in get_tool_specs()}

    @mcp.tool(
        name="incident_summary",
        description=_specs["incident_summary"].description,
    )
    async def incident_summary(
        incident_id: str,
        include_timeline: bool = True,
        include_affected_services: bool = True,
        execution_mode: str = "auto",
        workspace_id: str | None = None,
    ) -> str:
        """MCP tool wrapper for incident_summary."""
        mode = _resolve_execution_mode(settings, execution_mode)
        input_data = IncidentSummaryInput(
            incident_id=incident_id,
            include_timeline=include_timeline,
            include_affected_services=include_affected_services,
        )

        if mode == "sync":
            result: IncidentSummaryOutput = _incident_summary_impl(input_data)
            return result.model_dump_json(indent=2)

        client = PlatformAPIJobsClient(settings)
        submitted = await client.submit_job(
            {
                "job_type": "incident.summary.generate",
                "runner_mode": "summary",
                "task_profile": "summary.small",
                "workspace_id": workspace_id or "default",
                "incident_id": incident_id,
                "payload": input_data.model_dump(),
                "artifact_refs": [],
                "evidence_refs": [],
            }
        )
        return _build_async_result(
            job_id=submitted["job_id"],
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        )

    # ------------------------------------------------------------------
    # Tool: correlate_alerts
    # ------------------------------------------------------------------

    @mcp.tool(
        name="correlate_alerts",
        description=_specs["correlate_alerts"].description,
    )
    async def correlate_alerts(
        alerts_json: str,
        window_minutes: int = 60,
        min_cluster_size: int = 2,
        execution_mode: str = "auto",
        workspace_id: str | None = None,
    ) -> str:
        """
        MCP tool wrapper for correlate_alerts.

        alerts_json: JSON array of alert objects matching the Alert schema.
        """
        raw = json.loads(alerts_json)
        input_data = CorrelateAlertsInput(
            alerts=raw if isinstance(raw, list) else raw["alerts"],
            window_minutes=window_minutes,
            min_cluster_size=min_cluster_size,
        )
        mode = _resolve_execution_mode(settings, execution_mode)

        if mode == "sync":
            result: CorrelateAlertsOutput = _correlate_alerts_impl(input_data)
            return result.model_dump_json(indent=2)

        client = PlatformAPIJobsClient(settings)
        submitted = await client.submit_job(
            {
                "job_type": "incident.graph.build",
                "runner_mode": "graph",
                "task_profile": "graph.standard",
                "workspace_id": workspace_id or "default",
                "payload": {
                    "alerts": [a.model_dump(mode="json") for a in input_data.alerts],
                    "window_minutes": window_minutes,
                    "min_cluster_size": min_cluster_size,
                },
                "artifact_refs": [],
                "evidence_refs": [],
            }
        )
        return _build_async_result(
            job_id=submitted["job_id"],
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        )

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    register_resources(mcp)

    return mcp
