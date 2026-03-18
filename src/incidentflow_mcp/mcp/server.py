"""
MCP server definition.

Uses FastMCP (official MCP Python SDK) with Streamable HTTP transport.
All tools are registered here and wired to their implementation modules.
"""

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.config import Settings, get_settings
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
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "canceled"}
_VALID_RESPONSE_MODES = {"compact", "full"}


def _resolve_execution_mode(settings: Settings, requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    if mode == "auto":
        return "async" if settings.async_tools_enabled() else "sync"
    return mode


def _resolve_external_status_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    # This tool is runner-backed by design; keep behavior deterministic in local dev.
    return "async"


def _resolve_response_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_RESPONSE_MODES:
        raise ValueError(f"Unsupported response_mode: {requested_mode}")
    return mode


def _normalize_providers(providers: list[str] | None) -> list[str]:
    if not providers:
        return ["aws", "github"]

    allowed = {"aws", "github"}
    normalized = [item.strip().lower() for item in providers if item.strip()]
    if not normalized:
        return ["aws", "github"]

    invalid = [item for item in normalized if item not in allowed]
    if invalid:
        raise ValueError(f"Unsupported provider(s): {', '.join(invalid)}")

    seen: set[str] = set()
    ordered: list[str] = []
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


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


def _compact_incident(incident: Any) -> dict[str, Any]:
    if not isinstance(incident, dict):
        return {"name": str(incident)}

    return {
        "id": incident.get("id"),
        "name": incident.get("name") or incident.get("title"),
        "status": incident.get("status"),
        "impact": incident.get("impact"),
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "shortlink": incident.get("shortlink"),
    }


def _compact_degraded_component(component: Any) -> dict[str, Any]:
    if not isinstance(component, dict):
        return {"name": str(component)}

    return {
        "id": component.get("id"),
        "name": component.get("name"),
        "status": component.get("status"),
        "description": component.get("description"),
        "updated_at": component.get("updated_at"),
    }


def _compact_external_status_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result

    external_status = result.get("external_status")
    if not isinstance(external_status, list):
        return result

    compact_statuses: list[dict[str, Any]] = []
    for provider_status in external_status:
        if not isinstance(provider_status, dict):
            continue

        incidents_raw = provider_status.get("incidents")
        incidents_list = incidents_raw if isinstance(incidents_raw, list) else []
        compact_incidents = [_compact_incident(item) for item in incidents_list[:20]]

        degraded_raw = provider_status.get("degraded_components")
        degraded_list = degraded_raw if isinstance(degraded_raw, list) else []
        compact_degraded = [_compact_degraded_component(item) for item in degraded_list[:20]]

        compact_statuses.append(
            {
                "provider": provider_status.get("provider"),
                "indicator": provider_status.get("indicator"),
                "description": provider_status.get("description"),
                "incidents_total": len(incidents_list),
                "incidents": compact_incidents,
                "degraded_components": compact_degraded,
                "fetched_at": provider_status.get("fetched_at"),
                "truncated": len(incidents_list) > 20,
            }
        )

    return {
        "status": result.get("status"),
        "action": result.get("action"),
        "providers_succeeded": result.get("providers_succeeded"),
        "external_status": compact_statuses,
    }


def _normalize_polled_external_status_job(
    *,
    job_id: str,
    job: dict[str, Any],
    poll_after_seconds: int,
    response_mode: str,
) -> str:
    status = str(job.get("status", "unknown"))

    if status in {"admitted", "queued", "dispatched", "running"}:
        return _build_async_result(
            job_id=job_id,
            status=status,
            poll_after_seconds=poll_after_seconds,
        )

    if status in _TERMINAL_JOB_STATUSES:
        normalized_result = (
            _compact_external_status_result(job.get("result"))
            if response_mode == "compact"
            else job.get("result")
        )
        payload: dict[str, Any] = {
            "mode": "completed",
            "job_id": job_id,
            "status": status,
            "result": normalized_result,
            "error": job.get("error"),
            "artifact_refs": job.get("artifact_refs", []),
            "usage": job.get("usage"),
            "updated_at": job.get("updated_at"),
            "response_mode": response_mode,
        }
        return json.dumps(payload, indent=2)

    return _build_async_result(
        job_id=job_id,
        status=status,
        poll_after_seconds=poll_after_seconds,
    )


async def _poll_until_done(
    client: Any,
    job_id: str,
    initial_delay: int,
    max_wait_seconds: int = 45,
) -> dict[str, Any]:
    await asyncio.sleep(initial_delay)
    waited = initial_delay
    while waited < max_wait_seconds:
        job = await client.get_job(job_id)
        if str(job.get("status", "")) in _TERMINAL_JOB_STATUSES:
            return job
        interval = 3
        if waited + interval > max_wait_seconds:
            interval = max_wait_seconds - waited
        if interval <= 0:
            break
        await asyncio.sleep(interval)
        waited += interval
    return await client.get_job(job_id)


async def _execute_external_status_check(
    *,
    settings: Settings,
    client: Any,
    providers: list[str] | None,
    workspace_id: str | None,
    check_id: str | None,
    wait_for_result: bool = True,
    days_back: int = 30,
    response_mode: str = "compact",
) -> str:
    selected_response_mode = _resolve_response_mode(response_mode)
    selected_providers = _normalize_providers(providers)

    if check_id:
        job = await client.get_job(check_id)
        if wait_for_result and str(job.get("status", "")) not in _TERMINAL_JOB_STATUSES:
            job = await _poll_until_done(
                client=client,
                job_id=check_id,
                initial_delay=settings.platform_api_ai_poll_after_seconds,
                max_wait_seconds=45,
            )
        return _normalize_polled_external_status_job(
            job_id=check_id,
            job=job,
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            response_mode=selected_response_mode,
        )

    submitted = await client.submit_job(
        {
            "job_type": "alert.group.summary.generate",
            "runner_mode": "summary",
            "task_profile": "summary.small",
            "workspace_id": workspace_id or "default",
            "incident_id": "external-status",
            "payload": {
                "providers": selected_providers,
                "external_status_only": True,
                "days_back": days_back,
            },
            "artifact_refs": [],
            "evidence_refs": [],
        }
    )

    job_id = submitted["job_id"]

    if not wait_for_result:
        return _build_async_result(
            job_id=job_id,
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            extra={"providers": selected_providers},
        )

    job = await _poll_until_done(
        client=client,
        job_id=job_id,
        initial_delay=settings.platform_api_ai_poll_after_seconds,
        max_wait_seconds=45,
    )
    return _normalize_polled_external_status_job(
        job_id=job_id,
        job=job,
        poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        response_mode=selected_response_mode,
    )


def create_mcp_server() -> FastMCP:
    """
    Instantiate and configure the FastMCP server with all registered tools.

    Returns a FastMCP instance whose `streamable_http_app()` can be mounted
    into a FastAPI/Starlette application.
    """
    settings = get_settings()

    mcp = FastMCP(
        name=settings.mcp_server_name,
        host="0.0.0.0",
        stateless_http=True,
        streamable_http_path="/mcp",
    )

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

    @mcp.tool(
        name="external_status_check",
        description=_specs["external_status_check"].description,
    )
    async def external_status_check(
        providers: list[str] | None = None,
        execution_mode: str = "async",
        workspace_id: str | None = None,
        check_id: str | None = None,
        wait_for_result: bool = True,
        days_back: int = 30,
        response_mode: str = "compact",
    ) -> str:
        mode = _resolve_external_status_mode(execution_mode)
        if mode != "async":
            raise ValueError("external_status_check supports async orchestration only")

        client = PlatformAPIJobsClient(settings)
        return await _execute_external_status_check(
            settings=settings,
            client=client,
            providers=providers,
            workspace_id=workspace_id,
            check_id=check_id,
            wait_for_result=wait_for_result,
            days_back=days_back,
            response_mode=response_mode,
        )

    register_resources(mcp)

    return mcp
