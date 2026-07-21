"""Registration for async/job-backed MCP tools."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.services.async_jobs import (
    TERMINAL_JOB_STATUSES,
    build_async_result,
    execute_external_status_check,
    normalize_correlation_alerts,
    normalize_polled_incident_summary_job,
    poll_until_done,
    resolve_correlation_mode,
    resolve_execution_mode,
    resolve_external_status_mode,
    resolve_job_workspace_id,
)
from incidentflow_mcp.mcp.services.memory_context import MemoryContextService
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.tools.correlate_alerts import correlate_alerts as _correlate_alerts_impl
from incidentflow_mcp.tools.incident_summary import incident_summary as _incident_summary_impl
from incidentflow_mcp.tools.schemas import (
    Alert,
    CorrelateAlertsInput,
    CorrelateAlertsOutput,
    IncidentSummaryInput,
    IncidentSummaryOutput,
)

logger = logging.getLogger(__name__)


def register_async_tools(
    ctx: ToolRegistrationContext,
    *,
    memory: MemoryContextService,
    current_token_workspace_id: Callable[[], str | None],
) -> None:
    settings = ctx.settings
    mcp = ctx.mcp

    @mcp.tool(**ctx.metadata("incident_summary"))
    async def incident_summary(
        incident_id: str = "",
        include_timeline: bool = True,
        include_affected_services: bool = True,
        execution_mode: str = "auto",
        workspace_id: str | None = None,
        check_id: str | None = None,
        wait_for_result: bool = True,
    ) -> dict[str, Any]:
        # Poll/fetch an existing async summary job instead of creating a new one.
        if check_id:
            client = PlatformAPIJobsClient(settings)
            job = await client.get_job(check_id)
            if wait_for_result and str(job.get("status", "")) not in TERMINAL_JOB_STATUSES:
                job = await poll_until_done(
                    client=client,
                    job_id=check_id,
                    initial_delay=settings.platform_api_ai_poll_after_seconds,
                    max_wait_seconds=45,
                )
            return normalize_polled_incident_summary_job(
                job_id=check_id,
                job=job,
                poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            )

        if not incident_id.strip():
            raise ValueError("incident_id is required unless check_id is provided")

        mode = resolve_execution_mode(settings, execution_mode)
        input_data = IncidentSummaryInput(
            incident_id=incident_id,
            include_timeline=include_timeline,
            include_affected_services=include_affected_services,
        )

        resolved_workspace_id = resolve_job_workspace_id(
            workspace_id,
            token_workspace_id=current_token_workspace_id(),
            default_workspace_id=settings.mcp_default_workspace_id,
        )
        if mode == "sync":
            result: IncidentSummaryOutput = _incident_summary_impl(input_data)
            data = result.model_dump(mode="json")
            query = f"{result.title} {result.summary}".strip()
            service = result.affected_services[0] if result.affected_services else None
            memory_payload = await memory.consult_memory(
                query=query, service=service, workspace_id=workspace_id
            )
            if memory_payload:
                data["memory_context"] = memory_payload
            return data

        client = PlatformAPIJobsClient(settings)
        submitted = await client.submit_job(
            {
                "job_type": "incident.summary.generate",
                "runner_mode": "summary",
                "task_profile": "summary.small",
                "workspace_id": resolved_workspace_id,
                "incident_id": incident_id,
                "payload": input_data.model_dump(),
                "artifact_refs": [],
                "evidence_refs": [],
            }
        )
        logger.info(
            "mcp_async_job_submitted tool=incident_summary job_id=%s workspace_id=%s",
            submitted["job_id"],
            resolved_workspace_id,
        )
        return build_async_result(
            job_id=submitted["job_id"],
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        )

    @mcp.tool(**ctx.metadata("correlate_alerts"))
    async def correlate_alerts(
        alerts: Annotated[
            list[Alert] | None,
            Field(
                default=None,
                description=(
                    "Alert objects to correlate. Each alert requires alert_id, name, service, "
                    "severity, status, and fired_at; labels may include env, namespace, "
                    "pod, deployment, or other routing context."
                ),
            ),
        ] = None,
        alerts_json: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Legacy JSON string containing the same alert object array as alerts. "
                    "Prefer alerts for new calls."
                ),
            ),
        ] = None,
        window_minutes: int = 60,
        min_cluster_size: int = 2,
        execution_mode: str = "auto",
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_alerts = normalize_correlation_alerts(alerts, alerts_json)
        input_data = CorrelateAlertsInput(
            alerts=normalized_alerts,
            window_minutes=window_minutes,
            min_cluster_size=min_cluster_size,
        )
        resolve_correlation_mode(execution_mode)
        result: CorrelateAlertsOutput = _correlate_alerts_impl(input_data)

        data = result.model_dump(mode="json")
        # Consult memory using the alert names + dominant service as the signature.
        if normalized_alerts:
            names = [a.name for a in normalized_alerts if a.name]
            services = [a.service for a in normalized_alerts if a.service]
            dominant_service = max(set(services), key=services.count) if services else None
            query = " ".join(dict.fromkeys(names)) or "alert correlation"
            memory_payload = await memory.consult_memory(
                query=query, service=dominant_service, workspace_id=workspace_id
            )
            if memory_payload:
                data["memory_context"] = memory_payload
        return data

    @mcp.tool(**ctx.metadata("external_status_check"))
    async def external_status_check(
        providers: list[str] | None = None,
        execution_mode: str = "async",
        workspace_id: str | None = None,
        check_id: str | None = None,
        wait_for_result: bool = True,
        days_back: int = 30,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        mode = resolve_external_status_mode(execution_mode)
        if mode != "async":
            raise ValueError("external_status_check supports async orchestration only")

        client = PlatformAPIJobsClient(settings)
        return await execute_external_status_check(
            settings=settings,
            client=client,
            providers=providers,
            workspace_id=workspace_id,
            check_id=check_id,
            wait_for_result=wait_for_result,
            days_back=days_back,
            response_mode=response_mode,
            current_token_workspace_id=current_token_workspace_id,
        )
