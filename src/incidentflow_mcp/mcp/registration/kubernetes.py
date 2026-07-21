"""Registration for Kubernetes MCP tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from incidentflow_mcp.config import Settings
from incidentflow_mcp.integrations import ResolvedIntegrationContext, attach_integration_context
from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_guard_error as _structured_guard_error
from incidentflow_mcp.mcp.services import kubernetes_analysis as analysis
from incidentflow_mcp.mcp.services import kubernetes_commands as commands
from incidentflow_mcp.mcp.services.memory_context import MemoryContextService
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient

_K8S_ALLOWED_ACTIONS = {
    "k8s.list_namespaces",
    "k8s.list_pods",
    "k8s.get_pod",
    "k8s.get_pod_logs",
    "k8s.list_events",
    "k8s.list_deployments",
    "k8s.list_services",
    "k8s.get_rollout_status",
    "k8s.describe_pod",
}
_NO_CONNECTED_CLUSTER_MESSAGE = commands._NO_CONNECTED_CLUSTER_MESSAGE
_UNAUTHORIZED_CLUSTER_MESSAGE = commands._UNAUTHORIZED_CLUSTER_MESSAGE
_MISSING_NAMESPACE_MESSAGE = commands._MISSING_NAMESPACE_MESSAGE

_checked_at = commands._checked_at
_command_ok = commands._command_ok
_command_data = commands._command_data
_command_error = commands._command_error
_k8s_failed_response = commands._k8s_failed_response
_resolve_k8s_cluster_id = commands._resolve_k8s_cluster_id
_select_k8s_cluster_summary = commands._select_k8s_cluster_summary
_send_k8s_command = commands._send_k8s_command
_k8s_agent_status_payload = commands._k8s_agent_status_payload
_k8s_connection_health_payload = commands._k8s_connection_health_payload
_k8s_cluster_overview_payload = commands._k8s_cluster_overview_payload
_k8s_rbac_check_payload = commands._k8s_rbac_check_payload

_analyze_workload_logs = analysis._analyze_workload_logs
_build_describe_response = analysis._build_describe_response
_cluster_health_assessment = analysis._cluster_health_assessment
_compact_log_payload = analysis._compact_log_payload
_deduplicate_events = analysis._deduplicate_events
_diagnose_pod = analysis._diagnose_pod
_events_for_pod = analysis._events_for_pod
_filter_workload_pods = analysis._filter_workload_pods
_is_completed_pod = analysis._is_completed_pod
_is_unhealthy_pod = analysis._is_unhealthy_pod
_pod_restart_count = analysis._pod_restart_count
_sanitize_pod = analysis._sanitize_pod
_select_workload_pod_from_deployments = analysis._select_workload_pod_from_deployments
_sort_events_for_display = analysis._sort_events_for_display
_unhealthy_pod_entry = analysis._unhealthy_pod_entry
_workload_from_pod_name = analysis._workload_from_pod_name

ToolGuardResolver = Callable[[str], Awaitable[ResolvedIntegrationContext | str | None]]
BearerTokenResolver = Callable[[], str]


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2)


def _with_integration_context(
    payload: dict[str, Any],
    context: ResolvedIntegrationContext | None,
    settings: Settings,
) -> dict[str, Any]:
    """Attach shared-dev integration metadata while preserving structured output."""
    raw = attach_integration_context(_json(payload), context, settings)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "failed", "error": raw}
    return decoded if isinstance(decoded, dict) else {"status": "failed", "error": raw}


async def _send_k8s_agent_command(
    *,
    settings: Settings,
    current_bearer_token: BearerTokenResolver,
    cluster_id: str | None,
    action: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    environment: str | None = None,
    cluster_name: str | None = None,
    integration_context: ResolvedIntegrationContext | None = None,
) -> str:
    if action not in _K8S_ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported Kubernetes agent action: {action}")
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise ValueError("timeout_seconds must be between 1 and 60")

    client = PlatformAPIAgentCommandsClient(settings)
    bearer_token = current_bearer_token()
    effective_cluster_id = cluster_id
    if (
        integration_context
        and integration_context.source == "shared_dev"
        and not effective_cluster_id
    ):
        effective_cluster_id = integration_context.resource_id
    resolved_cluster_id = await _resolve_k8s_cluster_id(
        client=client,
        bearer_token=bearer_token,
        cluster_id=effective_cluster_id,
        environment=environment,
        cluster_name=cluster_name,
    )
    try:
        result = await client.send_agent_command(
            bearer_token=bearer_token,
            cluster_id=resolved_cluster_id,
            action=action,
            params=params or {},
            timeout_seconds=timeout_seconds,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise ValueError(_UNAUTHORIZED_CLUSTER_MESSAGE) from exc
        raise
    return attach_integration_context(json.dumps(result, indent=2), integration_context, settings)


async def _fetch_pods_for_analysis(
    *,
    settings: Settings,
    current_bearer_token: BearerTokenResolver,
    namespace: str | None,
    cluster_id: str | None,
    environment: str | None,
    cluster_name: str | None,
    timeout_seconds: int,
    integration_context: ResolvedIntegrationContext | None = None,
) -> dict[str, Any]:
    raw = await _send_k8s_agent_command(
        settings=settings,
        current_bearer_token=current_bearer_token,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
        action="k8s.list_pods",
        params={"namespace": namespace} if namespace else {},
        timeout_seconds=timeout_seconds,
        integration_context=integration_context,
    )
    return json.loads(raw)


def register_kubernetes_tools(
    ctx: ToolRegistrationContext,
    *,
    memory: MemoryContextService,
    resolve_tool_guard: ToolGuardResolver,
    current_bearer_token: BearerTokenResolver,
) -> None:
    settings = ctx.settings

    @ctx.mcp.tool(**ctx.metadata("k8s_connection_health"))
    async def k8s_connection_health(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_connection_health")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        client = PlatformAPIAgentCommandsClient(settings)
        return _with_integration_context(
            await _k8s_connection_health_payload(
                client=client,
                bearer_token=current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                timeout_seconds=timeout_seconds,
            ),
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_cluster_overview"))
    async def k8s_cluster_overview(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_cluster_overview")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = current_bearer_token()
        clusters = await client.list_clusters(bearer_token=bearer_token)
        cluster = _select_k8s_cluster_summary(
            clusters,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            connected_only=True,
        )
        if cluster is None:
            return {
                "status": "offline",
                "agent_online": False,
                "error": _NO_CONNECTED_CLUSTER_MESSAGE,
                "checked_at": _checked_at(),
            }
        overview = await _k8s_cluster_overview_payload(
            client=client,
            bearer_token=bearer_token,
            cluster_id=str(cluster["cluster_id"]),
            timeout_seconds=timeout_seconds,
        )
        health = _cluster_health_assessment(overview)
        overview.update(
            {
                "status": "connected",
                "cluster_id": cluster.get("cluster_id"),
                "cluster_name": cluster.get("name"),
                "cluster_health": health["cluster_health"],
                "summary": health["summary"],
                "findings": health["findings"],
                "recommendations": health["recommendations"],
            }
        )
        return _with_integration_context(
            overview,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_namespace_overview"))
    async def k8s_namespace_overview(
        namespace: str,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_namespace_overview")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = current_bearer_token()
        resolved_cluster_id = await _resolve_k8s_cluster_id(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
        )
        namespace_check = await _send_k8s_command(
            client=client,
            bearer_token=bearer_token,
            cluster_id=resolved_cluster_id,
            action="k8s.list_pods",
            params={"namespace": namespace},
            timeout_seconds=timeout_seconds,
        )
        if not _command_ok(namespace_check):
            namespace_check["cluster_id"] = resolved_cluster_id
            return _with_integration_context(
                namespace_check,
                guard if isinstance(guard, ResolvedIntegrationContext) else None,
                settings,
            )
        overview = await _k8s_cluster_overview_payload(
            client=client,
            bearer_token=bearer_token,
            cluster_id=resolved_cluster_id,
            namespace=namespace,
            timeout_seconds=timeout_seconds,
        )
        overview.update({"status": "connected", "cluster_id": resolved_cluster_id})
        return _with_integration_context(
            overview,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_rbac_check"))
    async def k8s_rbac_check(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_rbac_check")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = current_bearer_token()
        resolved_cluster_id = await _resolve_k8s_cluster_id(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
        )
        payload = await _k8s_rbac_check_payload(
            client=client,
            bearer_token=bearer_token,
            cluster_id=resolved_cluster_id,
            timeout_seconds=timeout_seconds,
        )
        payload["cluster_id"] = resolved_cluster_id
        return _with_integration_context(
            payload,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_agent_status"))
    async def k8s_agent_status(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_agent_status")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        _ = timeout_seconds
        client = PlatformAPIAgentCommandsClient(settings)
        return _with_integration_context(
            await _k8s_agent_status_payload(
                client=client,
                bearer_token=current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
            ),
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_list_namespaces"))
    async def k8s_list_namespaces(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_list_namespaces")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_namespaces",
            params={},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"status": "failed", "error": raw}

    @ctx.mcp.tool(**ctx.metadata("k8s_list_pods"))
    async def k8s_list_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        include_labels: bool = False,
        include_images: bool = True,
        include_node: bool = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_list_pods")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_pods",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        pods_raw = (data.get("pods") if isinstance(data, dict) else None) or []
        capped = pods_raw[: max(1, min(limit, 200))]
        pods_out = [
            _sanitize_pod(
                p,
                include_labels=include_labels,
                include_images=include_images,
                include_node=include_node,
            )
            for p in capped
            if isinstance(p, dict)
        ]
        return _with_integration_context(
            {
                "status": payload.get("status", "unknown"),
                "data": {
                    "pods": pods_out,
                    "count": len(pods_out),
                    "total": len(pods_raw),
                    "truncated": len(pods_raw) > len(capped),
                },
                "error": payload.get("error"),
            },
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_get_pod"))
    async def k8s_get_pod(
        namespace: str,
        pod: str,
        detail_level: Literal["summary", "standard", "debug"] = "summary",
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        include_labels: bool = False,
        include_images: bool = True,
        include_node: bool = True,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_get_pod")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        if detail_level not in {"summary", "standard", "debug"}:
            return {
                "status": "failed",
                "error": "detail_level must be one of: summary, standard, debug",
            }

        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_pod",
            params={"namespace": namespace, "pod": pod},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        pod_raw = data.get("pod") if isinstance(data, dict) else None
        if not isinstance(pod_raw, dict):
            return payload if isinstance(payload, dict) else {"status": "failed", "error": raw}

        sanitized = _sanitize_pod(
            pod_raw,
            include_labels=include_labels,
            include_images=include_images,
            include_node=include_node,
        )

        if detail_level == "summary":
            return _with_integration_context(
                {
                    "status": payload.get("status", "unknown"),
                    "data": {"pod": sanitized},
                    "error": payload.get("error"),
                },
                guard if isinstance(guard, ResolvedIntegrationContext) else None,
                settings,
            )

        # standard / debug — also fetch events for this pod
        events_raw_str = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_events",
            params={"namespace": namespace},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        events_payload = json.loads(events_raw_str)
        all_events = _command_data(events_payload, "events")
        pod_events = _sort_events_for_display(
            _deduplicate_events(_events_for_pod(all_events, pod))
        )[:15]

        result: dict[str, Any] = {
            "status": payload.get("status", "unknown"),
            "data": {
                "pod": sanitized,
                "events": [
                    {
                        "type": e.get("type"),
                        "reason": e.get("reason"),
                        "message": str(e.get("message") or "")[:200],
                        "count": e.get("count", 1),
                        "last_seen": e.get("last_seen") or e.get("lastSeen"),
                    }
                    for e in pod_events
                ],
                "diagnosis": _diagnose_pod(pod_raw, _events_for_pod(all_events, pod)),
            },
            "error": payload.get("error"),
        }
        if detail_level == "debug":
            result["data"]["_raw_agent_keys"] = list(pod_raw.keys())
        return _with_integration_context(
            result,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_get_pod_logs"))
    async def k8s_get_pod_logs(
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: Annotated[int, Field(ge=1, le=1000)] = 200,
        level: str | None = None,
        contains: str | None = None,
        exclude: str | None = None,
        since_minutes: int | None = None,
        compact: bool = True,
        json_parse: bool = False,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_get_pod_logs")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}
        if level is not None and level.lower().strip() not in {
            "trace",
            "debug",
            "info",
            "warn",
            "warning",
            "error",
            "critical",
            "fatal",
        }:
            return {
                "status": "failed",
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": (
                        "level must be one of: trace, debug, info, warn, warning, "
                        "error, critical, fatal"
                    ),
                },
            }
        if since_minutes is not None and not 1 <= since_minutes <= 10080:
            return {
                "status": "failed",
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "since_minutes must be between 1 and 10080",
                },
            }
        params: dict[str, Any] = {
            "namespace": namespace,
            "pod": pod,
            "tail_lines": tail_lines,
        }
        if container:
            params["container"] = container
        if since_minutes is not None:
            params["since_minutes"] = since_minutes
        if json_parse:
            params["json_parse"] = json_parse
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_pod_logs",
            params=params,
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        return _with_integration_context(
            _compact_log_payload(
                payload,
                level=level,
                contains=contains,
                exclude=exclude,
                compact=compact,
            ),
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_list_events"))
    async def k8s_list_events(
        namespace: str | None = None,
        pod: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_list_events")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_events",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        events_raw = (data.get("events") if isinstance(data, dict) else None) or []
        events = [e for e in events_raw if isinstance(e, dict)]
        if pod:
            events = _events_for_pod(events, pod)
        deduped = _deduplicate_events(events)
        sorted_events = _sort_events_for_display(deduped)
        capped = sorted_events[: max(1, min(limit, 200))]
        warning_count = sum(1 for e in capped if str(e.get("type") or "").lower() == "warning")
        return _with_integration_context(
            {
                "status": payload.get("status", "unknown"),
                "summary": (
                    f"{len(capped)} events ({warning_count} warnings)"
                    + (f" for pod {pod}" if pod else "")
                ),
                "data": {
                    "events": capped,
                    "count": len(capped),
                    "total": len(events),
                    "warning_count": warning_count,
                    "truncated": len(deduped) > len(capped),
                },
                "error": payload.get("error"),
            },
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_list_deployments"))
    async def k8s_list_deployments(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_list_deployments")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_deployments",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        deployments_raw = (data.get("deployments") if isinstance(data, dict) else None) or []
        capped = deployments_raw[: max(1, min(limit, 200))]
        return _with_integration_context(
            {
                "status": payload.get("status", "unknown"),
                "data": {
                    "deployments": capped,
                    "count": len(capped),
                    "total": len(deployments_raw),
                    "truncated": len(deployments_raw) > len(capped),
                },
                "error": payload.get("error"),
            },
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_list_services"))
    async def k8s_list_services(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_list_services")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_services",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        services_raw = (data.get("services") if isinstance(data, dict) else None) or []
        capped = services_raw[: max(1, min(limit, 200))]
        return _with_integration_context(
            {
                "status": payload.get("status", "unknown"),
                "data": {
                    "services": capped,
                    "count": len(capped),
                    "total": len(services_raw),
                    "truncated": len(services_raw) > len(capped),
                },
                "error": payload.get("error"),
            },
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_get_rollout_status"))
    async def k8s_get_rollout_status(
        namespace: str,
        deployment: str | None = None,
        workload: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_get_rollout_status")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}
        if deployment and workload and deployment != workload:
            return {
                "status": "failed",
                "error": (
                    f"deployment ({deployment!r}) and workload ({workload!r}) both provided "
                    "but differ; use only one."
                ),
            }
        resolved = deployment or workload
        if not resolved:
            return {"status": "failed", "error": "Either 'deployment' or 'workload' is required."}
        raw = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_rollout_status",
            params={"namespace": namespace, "deployment": resolved},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"status": "failed", "error": raw}

    @ctx.mcp.tool(**ctx.metadata("k8s_show_unhealthy_pods"))
    async def k8s_show_unhealthy_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        include_memory_context: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_show_unhealthy_pods")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        result = await _fetch_pods_for_analysis(
            current_bearer_token=current_bearer_token,
            settings=settings,
            namespace=namespace,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        data = result.get("data") if isinstance(result, dict) else None
        pods = data.get("pods") if isinstance(data, dict) else []
        pods = pods if isinstance(pods, list) else []
        unhealthy = [pod for pod in pods if isinstance(pod, dict) and _is_unhealthy_pod(pod)]
        completed = [pod for pod in pods if isinstance(pod, dict) and _is_completed_pod(pod)]
        unhealthy_entries = [_unhealthy_pod_entry(p) for p in unhealthy]
        findings: list[str] = (
            [f"⚠ {len(unhealthy)} unhealthy pod{'s' if len(unhealthy) != 1 else ''}"]
            + [f"  - {e['name']}: {e['reason']}" for e in unhealthy_entries[:5]]
            if unhealthy
            else ["✓ No unhealthy pods"]
        )
        recommendations: list[str] = list(
            dict.fromkeys(e["recommendation"] for e in unhealthy_entries if e.get("recommendation"))
        )
        report: dict[str, Any] = {
            "status": "success",
            "summary": (
                f"{len(unhealthy)} unhealthy pod{'s' if len(unhealthy) != 1 else ''}"
                if unhealthy
                else "All pods are healthy"
            ),
            "findings": findings,
            "recommendations": recommendations,
            "data": {
                "unhealthy_pods": unhealthy_entries,
                "count": len(unhealthy),
                "completed_jobs": [
                    _sanitize_pod(p, include_images=False, include_node=False) for p in completed
                ],
                "completed_count": len(completed),
            },
            "error": None,
        }
        # Consult memory only when there is a problem to match against.
        if include_memory_context and unhealthy:
            reasons = list(dict.fromkeys(e["reason"] for e in unhealthy_entries if e.get("reason")))
            query = " ".join(reasons + ([namespace] if namespace else [])) or "unhealthy pods"
            ctx = await memory.consult_memory(query=query, namespace=namespace)
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_analyze_workload"))
    async def k8s_analyze_workload(
        workload: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Deployment or Pod name to inspect, for example checkout-api or "
                    "checkout-api-7f9c6d7d8b-abcde. Do not include kind/ prefixes."
                ),
            ),
        ],
        namespace: Annotated[
            str,
            Field(min_length=1, description="Kubernetes namespace containing the workload."),
        ],
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        tail_lines: Annotated[int, Field(ge=1, le=1000)] = 100,
        include_memory_context: bool = True,
        include_raw_logs: bool = False,
        exclude_loggers: list[str] | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_analyze_workload")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}
        if not workload:
            return {
                "status": "failed",
                "error": "Please specify a pod or deployment name to analyze.",
            }

        rollout = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_rollout_status",
            params={"namespace": namespace, "deployment": workload},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        pods = await _fetch_pods_for_analysis(
            current_bearer_token=current_bearer_token,
            settings=settings,
            namespace=namespace,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        pods_data = pods.get("data") if isinstance(pods, dict) else None
        pod_items = pods_data.get("pods") if isinstance(pods_data, dict) else []
        pods_error = _command_error(pods)
        rollout_payload = json.loads(rollout)
        rollout_status = rollout_payload.get("data")
        rollout_inner = (rollout_status or {}).get("rollout") or {}
        rollout_error = _command_error(rollout_payload)
        deployments = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_deployments",
            params={"namespace": namespace},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )
        deployments_payload = json.loads(deployments)
        deployments_data = deployments_payload.get("data")
        deployments_error = _command_error(deployments_payload)
        deployment_items = (
            deployments_data.get("deployments") if isinstance(deployments_data, dict) else []
        )
        selected_pod = _select_workload_pod_from_deployments(
            pod_items if isinstance(pod_items, list) else [],
            deployment_items if isinstance(deployment_items, list) else [],
            workload,
        )
        related_pods = _filter_workload_pods(
            pod_items if isinstance(pod_items, list) else [],
            deployment_items if isinstance(deployment_items, list) else [],
            workload,
        )
        blocking_error = pods_error or deployments_error
        if blocking_error and not related_pods and not rollout_inner:
            code = str(blocking_error.get("code") or "K8S_LOOKUP_FAILED")
            message = str(
                blocking_error.get("message")
                or f"Unable to inspect namespace {namespace} for workload {workload}"
            )
            return _with_integration_context(
                _k8s_failed_response(
                    code=code,
                    message=message,
                    summary=f"Unable to inspect {workload} in namespace {namespace}",
                    details={"namespace": namespace, "workload": workload},
                ),
                guard if isinstance(guard, ResolvedIntegrationContext) else None,
                settings,
            )
        if not related_pods and not rollout_inner:
            code = str((rollout_error or {}).get("code") or "NOT_FOUND")
            if code in {"", "unknown"}:
                code = "NOT_FOUND"
            message = (
                f"No deployment or pods found for workload {workload} in namespace {namespace}"
            )
            return _with_integration_context(
                _k8s_failed_response(
                    code=code,
                    message=message,
                    summary=f"No matching workload found for {workload}",
                    details={
                        "namespace": namespace,
                        "workload": workload,
                        "rollout_status": rollout_status,
                        "pods": [],
                        "pods_total": 0,
                        "selected_pod": None,
                    },
                ),
                guard if isinstance(guard, ResolvedIntegrationContext) else None,
                settings,
            )
        logs_data = None
        log_analysis = _analyze_workload_logs(None, exclude_loggers=exclude_loggers)
        if selected_pod:
            logs = await _send_k8s_agent_command(
                settings=settings,
                current_bearer_token=current_bearer_token,
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                action="k8s.get_pod_logs",
                params={
                    "namespace": namespace,
                    "pod": selected_pod,
                    "tail_lines": tail_lines,
                },
                timeout_seconds=timeout_seconds,
                integration_context=guard
                if isinstance(guard, ResolvedIntegrationContext)
                else None,
            )
            logs_payload = json.loads(logs)
            logs_compact = _compact_log_payload(
                logs_payload, level=None, contains=None, exclude=None, compact=True
            )
            logs_data = logs_compact.get("data") if isinstance(logs_compact, dict) else None
            log_analysis = _analyze_workload_logs(
                logs_data if isinstance(logs_data, dict) else None,
                exclude_loggers=exclude_loggers,
            )
        rollout_complete = rollout_inner.get("complete")
        ready_pods = [
            p
            for p in related_pods
            if isinstance(p, dict)
            and str(p.get("phase") or "").lower() == "running"
            and bool([c for c in (p.get("containers") or []) if isinstance(c, dict)])
            and all(
                bool(c.get("ready")) for c in (p.get("containers") or []) if isinstance(c, dict)
            )
        ]
        total_restarts = sum(_pod_restart_count(p) for p in related_pods if isinstance(p, dict))
        unhealthy_related = [
            p for p in related_pods if isinstance(p, dict) and _is_unhealthy_pod(p)
        ]
        health = "healthy"
        severity = "info"
        if unhealthy_related or rollout_complete is False or log_analysis["errors"] > 0:
            health = "degraded"
            severity = "critical" if log_analysis["errors"] > 0 else "warning"
        elif log_analysis["warnings"] > 0 or total_restarts > 0:
            health = "warning"
            severity = "warning"

        findings: list[str] = []
        recommendations: list[str] = []
        if rollout_complete is True:
            findings.append("Deployment rollout is complete")
        elif rollout_complete is False:
            findings.append("Deployment rollout is not complete")
            recommendations.append("Check rollout events and deployment conditions")
        else:
            findings.append("Deployment rollout status is unknown")

        findings.append(f"{len(ready_pods)}/{len(related_pods)} pods are ready")
        if total_restarts == 0:
            findings.append("No restarts detected")
        else:
            suffix = "s" if total_restarts != 1 else ""
            findings.append(f"{total_restarts} restart{suffix} detected")
            recommendations.append(
                "Review pod restart timestamps and previous container termination reasons"
            )
        if log_analysis["errors"] == 0 and log_analysis["warnings"] == 0:
            findings.append(
                f"No error or warning logs found in the last {log_analysis['lines_scanned']} lines"
            )
        else:
            findings.append(
                f"{log_analysis['errors']} error and "
                f"{log_analysis['warnings']} warning log lines found"
            )
            recommendations.append("Inspect notable log lines and dependency failures")

        summary_state = "healthy" if health == "healthy" else health
        summary = f"{workload} is {summary_state}"
        report: dict[str, Any] = {
            "status": "success",
            "summary": summary,
            "health": health,
            "severity": severity,
            "findings": findings,
            "recommendations": list(dict.fromkeys(recommendations)),
            "data": {
                "rollout_status": rollout_status,
                "pods": [
                    _sanitize_pod(p, include_images=False, include_node=False)
                    for p in related_pods
                    if isinstance(p, dict)
                ],
                "pods_total": len(related_pods),
                "log_analysis": log_analysis,
                "raw_logs": logs_data if include_raw_logs else None,
                "selected_pod": selected_pod,
                "hint": (
                    "No matching pod was found for logs."
                    if selected_pod is None
                    else "Logs are from the selected pod."
                ),
                "tail_lines": tail_lines,
            },
            "error": None,
        }
        # Consult memory only when the workload looks unhealthy (bad rollout or bad pods).
        if include_memory_context and (unhealthy_related or rollout_inner.get("complete") is False):
            ctx = await memory.consult_memory(
                query=f"{workload} {namespace} rollout pod failure", namespace=namespace
            )
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_describe_pod"))
    async def k8s_describe_pod(
        namespace: str,
        pod: str,
        include_details: bool = False,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        include_memory_context: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_describe_pod")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}

        raw_str = await _send_k8s_agent_command(
            current_bearer_token=current_bearer_token,
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.describe_pod",
            params={"namespace": namespace, "pod": pod},
            timeout_seconds=timeout_seconds,
            integration_context=guard if isinstance(guard, ResolvedIntegrationContext) else None,
        )

        payload = json.loads(raw_str)
        data = payload.get("data") if isinstance(payload, dict) else None
        desc = data.get("description") if isinstance(data, dict) else None
        if not isinstance(desc, dict):
            return payload if isinstance(payload, dict) else {"status": "failed", "error": raw_str}

        describe = _build_describe_response(desc, include_details=include_details)
        if include_memory_context:
            ctx = await memory.consult_pod_memory(describe, pod=pod, namespace=namespace)
            if ctx:
                describe["memory_context"] = ctx
        return _with_integration_context(
            describe,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @ctx.mcp.tool(**ctx.metadata("k8s_debug_pod"))
    async def k8s_debug_pod(
        namespace: str,
        pod: str,
        tail_lines: Annotated[int, Field(ge=1, le=500)] = 100,
        include_evidence_details: bool = False,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        include_memory_context: bool = True,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("k8s_debug_pod")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}

        describe_str, logs_raw_str = await asyncio.gather(
            _send_k8s_agent_command(
                settings=settings,
                current_bearer_token=current_bearer_token,
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                action="k8s.describe_pod",
                params={"namespace": namespace, "pod": pod},
                timeout_seconds=timeout_seconds,
                integration_context=guard
                if isinstance(guard, ResolvedIntegrationContext)
                else None,
            ),
            _send_k8s_agent_command(
                settings=settings,
                current_bearer_token=current_bearer_token,
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                action="k8s.get_pod_logs",
                params={"namespace": namespace, "pod": pod, "tail_lines": tail_lines},
                timeout_seconds=timeout_seconds,
                integration_context=guard
                if isinstance(guard, ResolvedIntegrationContext)
                else None,
            ),
        )

        desc_payload = json.loads(describe_str)
        desc_data = desc_payload.get("data") if isinstance(desc_payload, dict) else None
        desc = desc_data.get("description") if isinstance(desc_data, dict) else None
        if not isinstance(desc, dict):
            return (
                desc_payload
                if isinstance(desc_payload, dict)
                else {"status": "failed", "error": describe_str}
            )

        describe = _build_describe_response(desc, include_details=include_evidence_details)
        diagnosis = describe["data"]["diagnosis"]

        logs_payload = json.loads(logs_raw_str)
        logs_data = _compact_log_payload(
            logs_payload, level=None, contains=None, exclude=None, compact=True
        )
        log_lines = logs_data.get("data") or {}
        highlighted = log_lines.get("highlighted") or []
        recent_lines = log_lines.get("lines") or []

        # Conditionally fetch rollout status if owner is a Deployment/ReplicaSet
        rollout_complete: bool | None = None
        owner = str((desc.get("metadata") or {}).get("owner") or "")
        if owner.lower().startswith("deployment/") or owner.lower().startswith("replicaset/"):
            workload_name = _workload_from_pod_name(pod)
            try:
                rollout_str = await _send_k8s_agent_command(
                    settings=settings,
                    current_bearer_token=current_bearer_token,
                    cluster_id=cluster_id,
                    environment=environment,
                    cluster_name=cluster_name,
                    action="k8s.get_rollout_status",
                    params={"namespace": namespace, "deployment": workload_name},
                    timeout_seconds=timeout_seconds,
                    integration_context=guard
                    if isinstance(guard, ResolvedIntegrationContext)
                    else None,
                )
                rollout_payload = json.loads(rollout_str)
                rollout_inner = (rollout_payload.get("data") or {}).get("rollout") or {}
                rollout_complete = bool(rollout_inner.get("complete"))
            except Exception:
                pass

        # --- Build compact SRE report ---
        pod_meta = describe["data"]["pod"]
        pod_status = describe["data"]["status"]
        containers = describe["data"]["containers"]
        historical = diagnosis.get("historical_warnings") or []
        current_issues = diagnosis.get("current_issues") or []
        observations = describe.get("observations") or describe["data"].get("observations") or []
        next_actions = describe.get("next_actions") or describe["data"].get("next_actions") or []

        total_restarts = int(pod_status.get("restart_count") or 0)
        pod_ready = bool(pod_status.get("ready"))
        log_error_count = len(
            [
                line
                for line in (recent_lines or [])
                if isinstance(line, str)
                and any(kw in line.lower() for kw in ("error", "exception", "fatal", "panic"))
            ]
        )
        log_warning_count = len(
            [
                line
                for line in (recent_lines or [])
                if isinstance(line, str) and "warn" in line.lower()
            ]
        )

        # Latest warning event age in minutes
        latest_warning_age_minutes: int | None = None
        warning_reasons: list[str] = []
        for w in historical:
            warning_reasons.append(w.get("type", ""))
        for i in current_issues:
            warning_reasons.append(i.get("type", ""))

        findings = list(describe["findings"])
        if highlighted:
            findings.append(
                f"⚠ {len(highlighted)} log line{'s' if len(highlighted) != 1 else ''}"
                " with errors/warnings"
            )
        elif recent_lines:
            findings.append(f"✓ No error/warning patterns in {len(recent_lines)} sampled log lines")
        if rollout_complete is True:
            findings.append("✓ Deployment rollout is complete")
        elif rollout_complete is False:
            findings.append("⚠ Deployment rollout is not complete")

        recommendations = list(describe["recommendations"])
        if not current_issues and pod_ready and total_restarts == 0:
            recommendations = recommendations or ["No immediate action needed"]

        evidence: dict[str, Any] = {
            "pod_ready": pod_ready,
            "phase": pod_status.get("phase"),
            "restart_count": total_restarts,
            "last_restart_at": pod_status.get("last_restart_at"),
            "restarts_last_1h": pod_status.get("restarts_last_1h", 0),
            "restarts_last_24h": pod_status.get("restarts_last_24h", 0),
            "rollout_complete": rollout_complete,
            "warning_reasons": list(dict.fromkeys(warning_reasons)),
            "log_error_count": log_error_count,
            "log_warning_count": log_warning_count,
            "latest_warning_age_minutes": latest_warning_age_minutes,
            "observations": observations,
        }
        if include_evidence_details:
            evidence["highlighted_log_lines"] = highlighted[-10:]
            evidence["containers"] = [
                {
                    "name": c.get("name"),
                    "ready": c.get("ready"),
                    "restart_count": c.get("restart_count"),
                    "last_restart_at": c.get("last_restart_at"),
                    "state": c.get("state"),
                }
                for c in containers
                if isinstance(c, dict)
            ]
            evidence["events"] = [
                {
                    "type": e.get("type"),
                    "reason": e.get("reason"),
                    "message": str(e.get("message") or "")[:120],
                    "count": e.get("count"),
                    "last_seen": e.get("last_seen"),
                }
                for e in describe["data"].get("events", [])[:10]
            ]
            evidence["node"] = pod_meta.get("node")

        report: dict[str, Any] = {
            "status": "success",
            "summary": describe["summary"],
            "findings": findings,
            "observations": observations,
            "recommendations": recommendations,
            "next_actions": next_actions,
            "evidence": evidence,
        }
        if include_memory_context:
            ctx = await memory.consult_pod_memory(describe, pod=pod, namespace=namespace)
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )
