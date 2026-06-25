"""
MCP server definition.

Uses FastMCP (official MCP Python SDK) with Streamable HTTP transport.
All tools are registered here and wired to their implementation modules.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.config import Settings, get_settings
from incidentflow_mcp.mcp.resources import register_resources
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.platform_api.slack_client import PlatformSlackAPIError, PlatformSlackClient
from incidentflow_mcp.tools.correlate_alerts import correlate_alerts as _correlate_alerts_impl
from incidentflow_mcp.tools.incident_summary import incident_summary as _incident_summary_impl
from incidentflow_mcp.tools.registry import get_tool_specs
from incidentflow_mcp.tools.schemas import (
    Alert,
    CorrelateAlertsInput,
    CorrelateAlertsOutput,
    IncidentSummaryInput,
    IncidentSummaryOutput,
)
from incidentflow_mcp.tools.slack_alerts import (
    fetch_slack_alert_thread,
    fetch_slack_alerts,
    summarize_incident_thread,
)

logger = logging.getLogger(__name__)


class IncidentThreadAlertContext(BaseModel):
    alert_name: str | None = Field(
        default=None,
        description="Alert name from the root Slack alert, for example InstanceDown.",
    )
    name: str | None = Field(default=None, description="Alternative alert name field.")
    summary: str | None = Field(default=None, description="Short alert or incident summary.")
    service: str | None = Field(default=None, description="Affected service name.")
    severity: str | None = Field(
        default=None,
        description="Alert severity such as critical or warning.",
    )
    status: str | None = Field(default=None, description="Alert status such as firing or resolved.")
    labels: dict[str, str] | None = Field(
        default=None,
        description="Alert labels copied from Grafana, Alertmanager, or IncidentFlow.",
    )


class K8sAgentCommandParams(BaseModel):
    namespace: str | None = Field(
        default=None,
        description=(
            "Kubernetes namespace for namespaced actions such as list_pods, get_pod, "
            "get_pod_logs, list_events, list_deployments, list_services, or get_rollout_status."
        ),
    )
    pod: str | None = Field(
        default=None,
        description="Pod name for k8s.get_pod or k8s.get_pod_logs.",
    )
    container: str | None = Field(
        default=None,
        description="Optional container name for k8s.get_pod_logs.",
    )
    deployment: str | None = Field(
        default=None,
        description="Deployment name for k8s.get_rollout_status.",
    )
    tail_lines: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Maximum recent log lines for k8s.get_pod_logs.",
    )


K8sReadOnlyAction = Literal[
    "k8s.list_namespaces",
    "k8s.list_pods",
    "k8s.get_pod",
    "k8s.get_pod_logs",
    "k8s.list_events",
    "k8s.list_deployments",
    "k8s.list_services",
    "k8s.get_rollout_status",
]

_VALID_EXECUTION_MODES = {"auto", "sync", "async"}
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "canceled"}
_VALID_RESPONSE_MODES = {"compact", "full"}
_VALID_SLACK_THREAD_MODES = {"none", "metadata", "full"}
_K8S_ALLOWED_ACTIONS = {
    "k8s.list_namespaces",
    "k8s.list_pods",
    "k8s.get_pod",
    "k8s.get_pod_logs",
    "k8s.list_events",
    "k8s.list_deployments",
    "k8s.list_services",
    "k8s.get_rollout_status",
}
_NO_CONNECTED_CLUSTER_MESSAGE = (
    "No Kubernetes cluster is connected to this workspace. "
    "Connect a cluster in Integrations -> Kubernetes first."
)
_MULTIPLE_CLUSTERS_MESSAGE = (
    "Multiple Kubernetes clusters are connected. Please specify environment, "
    "for example production, staging, or dev."
)
_UNAUTHORIZED_CLUSTER_MESSAGE = (
    "You are not authorized to access this Kubernetes cluster or namespace."
)
_MISSING_NAMESPACE_MESSAGE = "Please specify a namespace, or use list_namespaces first."
_K8S_RBAC_ACTIONS = {
    "list_namespaces": ("k8s.list_namespaces", {}),
    "list_pods": ("k8s.list_pods", {}),
    "list_events": ("k8s.list_events", {}),
    "list_deployments": ("k8s.list_deployments", {}),
    "list_services": ("k8s.list_services", {}),
}
_SLACK_THREAD_MODE_ALIASES = {
    "summarize": "full",
    "summary": "full",
    "analysis": "full",
    "analyze": "full",
}


def _tool_metadata(spec: Any) -> dict[str, Any]:
    return {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "annotations": spec.annotations,
    }


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


def _resolve_correlation_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    if mode == "async":
        raise ValueError(
            "correlate_alerts async mode is disabled until a dedicated "
            "alert.correlation.generate runner exists; use execution_mode=sync or auto"
        )
    return "sync"


def _resolve_response_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in _VALID_RESPONSE_MODES:
        raise ValueError(f"Unsupported response_mode: {requested_mode}")
    return mode


def _normalize_slack_thread_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    mode = _SLACK_THREAD_MODE_ALIASES.get(mode, mode)
    if mode not in _VALID_SLACK_THREAD_MODES:
        raise ValueError(
            "thread_mode must be one of: none, metadata, full "
            "(summarize and analysis are accepted aliases for full)"
        )
    return mode


def _current_token_workspace_id() -> str | None:
    auth_context = get_current_auth_context()
    if not auth_context:
        return None
    workspace_id = auth_context.get("workspace_id")
    if workspace_id is None:
        return None
    normalized = str(workspace_id).strip()
    return normalized or None


def _current_bearer_token() -> str:
    auth_context = get_current_auth_context()
    if not auth_context:
        raise ValueError("Authenticated MCP request context is required")
    token = (auth_context.get("bearer_token") or "").strip()
    if not token:
        raise ValueError("Bearer token is required for Kubernetes agent tools")
    return token


def _tool_error_json(code: str, message: str, **details: object) -> str:
    payload: dict[str, object] = {"error": code, "code": code, "message": message}
    if details:
        payload["details"] = details
    return json.dumps(payload, indent=2)


def _workspace_context_required_error() -> str:
    return _tool_error_json(
        "mcp_workspace_context_required",
        (
            "MCP Slack tools require an OAuth or workspace token with workspace_id. "
            "Authorize the MCP client through IncidentFlow OAuth and retry."
        ),
    )


def _platform_slack_error_json(exc: PlatformSlackAPIError) -> str:
    return _tool_error_json(exc.code, exc.message)


def _normalize_k8s_environment(environment: str | None) -> str | None:
    if environment is None:
        return None
    normalized = environment.strip().lower()
    if not normalized:
        return None
    aliases = {
        "prod": "production",
        "production": "production",
        "stage": "staging",
        "staging": "staging",
        "dev": "dev",
        "development": "dev",
    }
    return aliases.get(normalized, normalized)


def _cluster_search_values(cluster: dict[str, Any]) -> set[str]:
    values = {str(cluster.get("name") or "").strip().lower()}
    environment = _normalize_k8s_environment(str(cluster.get("environment") or ""))
    if environment:
        values.add(environment)
    aliases = cluster.get("aliases")
    if isinstance(aliases, list):
        values.update(str(item).strip().lower() for item in aliases if str(item).strip())
    return {item for item in values if item}


async def _resolve_k8s_cluster_id(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str | None = None,
    environment: str | None = None,
    cluster_name: str | None = None,
) -> str:
    explicit_cluster_id = cluster_id.strip() if cluster_id is not None else ""
    if explicit_cluster_id:
        return explicit_cluster_id

    try:
        clusters = await client.list_clusters(bearer_token=bearer_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise ValueError(_UNAUTHORIZED_CLUSTER_MESSAGE) from exc
        raise

    connected = [item for item in clusters if item.get("connected") is True]
    if not connected:
        raise ValueError(_NO_CONNECTED_CLUSTER_MESSAGE)

    wanted_environment = _normalize_k8s_environment(environment)
    wanted_name = cluster_name.strip().lower() if cluster_name is not None else ""

    matches = connected
    if wanted_environment:
        matches = [
            item
            for item in matches
            if _normalize_k8s_environment(str(item.get("environment") or "")) == wanted_environment
            or wanted_environment in _cluster_search_values(item)
        ]
    if wanted_name:
        matches = [item for item in matches if wanted_name in _cluster_search_values(item)]

    if not wanted_environment and not wanted_name and len(matches) == 1:
        return str(matches[0]["cluster_id"])

    if not matches:
        raise ValueError(_NO_CONNECTED_CLUSTER_MESSAGE)

    if len(matches) > 1:
        raise ValueError(_MULTIPLE_CLUSTERS_MESSAGE)

    return str(matches[0]["cluster_id"])


async def _dispatch_k8s_agent_command(
    *,
    settings: Settings,
    cluster_id: str | None,
    action: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    environment: str | None = None,
    cluster_name: str | None = None,
) -> str:
    if action not in _K8S_ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported Kubernetes agent action: {action}")
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise ValueError("timeout_seconds must be between 1 and 60")

    client = PlatformAPIAgentCommandsClient(settings)
    bearer_token = _current_bearer_token()
    resolved_cluster_id = await _resolve_k8s_cluster_id(
        client=client,
        bearer_token=bearer_token,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
    )
    try:
        result = await client.dispatch(
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
    return json.dumps(result, indent=2)


async def _dispatch_k8s_pods_for_analysis(
    *,
    settings: Settings,
    namespace: str | None,
    cluster_id: str | None,
    environment: str | None,
    cluster_name: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    raw = await _dispatch_k8s_agent_command(
        settings=settings,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
        action="k8s.list_pods",
        params={"namespace": namespace} if namespace else {},
        timeout_seconds=timeout_seconds,
    )
    return json.loads(raw)


def _checked_at() -> str:
    return datetime.now(tz=UTC).isoformat()


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2)


def _command_ok(response: dict[str, Any]) -> bool:
    return str(response.get("status") or "") == "succeeded" and response.get("error") is None


def _command_data(response: dict[str, Any], key: str) -> list[Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    return value if isinstance(value, list) else []


def _command_error(response: dict[str, Any]) -> dict[str, Any] | None:
    error = response.get("error")
    return error if isinstance(error, dict) else None


def _permission_result(response: dict[str, Any]) -> dict[str, Any]:
    error = _command_error(response)
    return {
        "allowed": _command_ok(response),
        "error_code": str(error.get("code")) if error else None,
        "message": str(error.get("message")) if error else None,
    }


def _namespace_names(items: list[Any]) -> list[str]:
    names = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return sorted(set(names))


def _container_restart_count(container: dict[str, Any]) -> int:
    try:
        return int(container.get("restart_count") or container.get("restartCount") or 0)
    except (TypeError, ValueError):
        return 0


def _pod_restart_count(pod: dict[str, Any]) -> int:
    containers = pod.get("containers")
    if not isinstance(containers, list):
        return 0
    return sum(
        _container_restart_count(container)
        for container in containers
        if isinstance(container, dict)
    )


def _pod_brief(pod: dict[str, Any]) -> dict[str, Any]:
    return {
        "namespace": pod.get("namespace"),
        "pod": pod.get("name"),
        "phase": pod.get("phase"),
        "node": pod.get("node_name") or pod.get("nodeName"),
        "restarts": _pod_restart_count(pod),
    }


def _top_restarts(pods: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    rows = [
        _pod_brief(pod) for pod in pods if isinstance(pod, dict) and _pod_restart_count(pod) > 0
    ]
    rows.sort(key=lambda item: int(item.get("restarts") or 0), reverse=True)
    return rows[:limit]


def _warning_events(events: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    warnings = [
        event
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "").lower() == "warning"
    ]
    warnings.sort(
        key=lambda item: str(item.get("last_seen") or item.get("lastSeen") or ""),
        reverse=True,
    )
    return warnings[:limit]


def _cluster_matches(
    cluster: dict[str, Any],
    *,
    cluster_id: str | None,
    environment: str | None,
    cluster_name: str | None,
) -> bool:
    if cluster_id and str(cluster.get("cluster_id") or "") != cluster_id.strip():
        return False
    wanted_environment = _normalize_k8s_environment(environment)
    if wanted_environment and wanted_environment not in _cluster_search_values(cluster):
        return False
    wanted_name = cluster_name.strip().lower() if cluster_name else ""
    if wanted_name and wanted_name not in _cluster_search_values(cluster):
        return False
    return True


def _select_k8s_cluster_summary(
    clusters: list[dict[str, Any]],
    *,
    cluster_id: str | None = None,
    environment: str | None = None,
    cluster_name: str | None = None,
    connected_only: bool = False,
) -> dict[str, Any] | None:
    matches = [
        cluster
        for cluster in clusters
        if _cluster_matches(
            cluster,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
        )
    ]
    if connected_only:
        matches = [cluster for cluster in matches if cluster.get("connected") is True]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(_MULTIPLE_CLUSTERS_MESSAGE)
    return matches[0]


async def _dispatch_k8s_agent_command_json(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str,
    action: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    try:
        return await client.dispatch(
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action=action,
            params=params or {},
            timeout_seconds=timeout_seconds,
        )
    except httpx.HTTPStatusError as exc:
        response = exc.response
        return {
            "status": "failed",
            "error": {
                "code": f"http_{response.status_code}",
                "message": response.text[:500],
            },
        }
    except httpx.HTTPError as exc:
        return {
            "status": "failed",
            "error": {
                "code": "platform_api_unreachable",
                "message": str(exc),
            },
        }


async def _k8s_agent_status_payload(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str | None = None,
    environment: str | None = None,
    cluster_name: str | None = None,
) -> dict[str, Any]:
    try:
        clusters = await client.list_clusters(bearer_token=bearer_token)
    except httpx.HTTPError as exc:
        return {
            "status": "offline",
            "agent_online": False,
            "checked_at": _checked_at(),
            "error": str(exc),
        }
    cluster = _select_k8s_cluster_summary(
        clusters,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
        connected_only=False,
    )
    if cluster is None:
        return {
            "status": "offline",
            "agent_online": False,
            "clusters": clusters,
            "checked_at": _checked_at(),
            "error": "No Kubernetes cluster matched the requested selector.",
        }
    return {
        "status": "connected" if cluster.get("connected") is True else "offline",
        "cluster_id": cluster.get("cluster_id"),
        "cluster_name": cluster.get("name"),
        "environment": cluster.get("environment"),
        "agent_id": cluster.get("agent_id"),
        "agent_version": cluster.get("agent_version"),
        "agent_status": cluster.get("agent_status"),
        "agent_online": cluster.get("connected") is True,
        "last_seen_at": cluster.get("last_seen_at"),
        "last_heartbeat_at": cluster.get("last_heartbeat_at"),
        "checked_at": _checked_at(),
    }


async def _k8s_connection_health_payload(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str | None = None,
    environment: str | None = None,
    cluster_name: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    status = await _k8s_agent_status_payload(
        client=client,
        bearer_token=bearer_token,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
    )
    resolved_cluster_id = status.get("cluster_id")
    if not resolved_cluster_id or status.get("agent_online") is not True:
        status.update(
            {
                "latency_ms": None,
                "namespaces_visible": 0,
                "namespaces": [],
                "permissions": {
                    "list_namespaces": False,
                    "list_pods": None,
                    "get_logs": None,
                    "list_events": None,
                    "list_deployments": None,
                    "list_services": None,
                },
                "read_only": True,
            }
        )
        return status

    started = time.perf_counter()
    namespaces_response = await _dispatch_k8s_agent_command_json(
        client=client,
        bearer_token=bearer_token,
        cluster_id=str(resolved_cluster_id),
        action="k8s.list_namespaces",
        params={},
        timeout_seconds=timeout_seconds,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    namespaces = _namespace_names(_command_data(namespaces_response, "namespaces"))
    permissions: dict[str, bool | None] = {
        "list_namespaces": _command_ok(namespaces_response),
        "list_pods": None,
        "get_logs": None,
        "list_events": None,
        "list_deployments": None,
        "list_services": None,
    }

    if namespaces:
        namespace = namespaces[0]
        checks = {
            "list_pods": ("k8s.list_pods", {"namespace": namespace}),
            "list_events": ("k8s.list_events", {"namespace": namespace}),
            "list_deployments": ("k8s.list_deployments", {"namespace": namespace}),
            "list_services": ("k8s.list_services", {"namespace": namespace}),
        }
        pod_items: list[Any] = []
        for key, (action, params) in checks.items():
            response = await _dispatch_k8s_agent_command_json(
                client=client,
                bearer_token=bearer_token,
                cluster_id=str(resolved_cluster_id),
                action=action,
                params=params,
                timeout_seconds=timeout_seconds,
            )
            permissions[key] = _command_ok(response)
            if key == "list_pods":
                pod_items = _command_data(response, "pods")
        first_pod = next((pod for pod in pod_items if isinstance(pod, dict)), None)
        if first_pod is not None:
            response = await _dispatch_k8s_agent_command_json(
                client=client,
                bearer_token=bearer_token,
                cluster_id=str(resolved_cluster_id),
                action="k8s.get_pod_logs",
                params={
                    "namespace": first_pod.get("namespace") or namespace,
                    "pod": first_pod.get("name"),
                    "tail_lines": 1,
                },
                timeout_seconds=timeout_seconds,
            )
            permissions["get_logs"] = _command_ok(response)

    status.update(
        {
            "status": "connected" if _command_ok(namespaces_response) else "degraded",
            "latency_ms": latency_ms,
            "namespaces_visible": len(namespaces),
            "namespaces": namespaces,
            "permissions": permissions,
            "read_only": True,
            "checked_at": _checked_at(),
        }
    )
    return status


def _overview_payload(
    *,
    namespaces: list[str],
    pods: list[Any],
    deployments: list[Any],
    services: list[Any],
    events: list[Any],
    namespace: str | None = None,
) -> dict[str, Any]:
    running_pods = [
        pod
        for pod in pods
        if isinstance(pod, dict) and str(pod.get("phase") or "").lower() == "running"
    ]
    completed = [pod for pod in pods if isinstance(pod, dict) and _is_completed_pod(pod)]
    unhealthy = [
        pod
        for pod in pods
        if isinstance(pod, dict) and not _is_completed_pod(pod) and _is_unhealthy_pod(pod)
    ]
    warnings = _warning_events(events)
    return {
        "namespace": namespace,
        "namespaces": len(namespaces),
        "pods_total": len(pods),
        "pods_running": len(running_pods),
        "pods_unhealthy": len(unhealthy),
        "deployments": len(deployments),
        "services": len(services),
        "recent_warning_events": len(
            [
                event
                for event in events
                if isinstance(event, dict) and str(event.get("type") or "").lower() == "warning"
            ]
        ),
        "top_restarts": _top_restarts(pods),
        "unhealthy_pods": [_pod_brief(pod) for pod in unhealthy[:20]],
        "completed_jobs": [_pod_brief(pod) for pod in completed[:20]],
        "warning_events": warnings,
        "checked_at": _checked_at(),
    }


async def _k8s_cluster_overview_payload(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str,
    namespace: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    namespaces_response = await _dispatch_k8s_agent_command_json(
        client=client,
        bearer_token=bearer_token,
        cluster_id=cluster_id,
        action="k8s.list_namespaces",
        params={},
        timeout_seconds=timeout_seconds,
    )
    namespaces = _namespace_names(_command_data(namespaces_response, "namespaces"))
    if namespace:
        namespaces = [namespace]

    pods: list[Any] = []
    deployments: list[Any] = []
    services: list[Any] = []
    events: list[Any] = []
    for item in namespaces:
        pods_response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_pods",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        deployments_response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_deployments",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        services_response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_services",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        events_response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_events",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        pods.extend(_command_data(pods_response, "pods"))
        deployments.extend(_command_data(deployments_response, "deployments"))
        services.extend(_command_data(services_response, "services"))
        events.extend(_command_data(events_response, "events"))

    return _overview_payload(
        namespaces=namespaces,
        pods=pods,
        deployments=deployments,
        services=services,
        events=events,
        namespace=namespace,
    )


async def _k8s_rbac_check_payload(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    namespace: str | None = None
    first_pod: dict[str, Any] | None = None
    for key, (action, params) in _K8S_RBAC_ACTIONS.items():
        response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action=action,
            params=params if namespace is None else {**params, "namespace": namespace},
            timeout_seconds=timeout_seconds,
        )
        results[key] = _permission_result(response)
        if key == "list_namespaces":
            names = _namespace_names(_command_data(response, "namespaces"))
            namespace = names[0] if names else None
        if key == "list_pods":
            pods = _command_data(response, "pods")
            first_pod = next((pod for pod in pods if isinstance(pod, dict)), None)

    if first_pod is None:
        results["get_logs"] = {
            "allowed": None,
            "error_code": None,
            "message": "No visible pods available to verify log access.",
        }
    else:
        response = await _dispatch_k8s_agent_command_json(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.get_pod_logs",
            params={
                "namespace": first_pod.get("namespace") or namespace,
                "pod": first_pod.get("name"),
                "tail_lines": 1,
            },
            timeout_seconds=timeout_seconds,
        )
        results["get_logs"] = _permission_result(response)

    return {"read_only": True, "permissions": results, "checked_at": _checked_at()}


def _is_unhealthy_pod(pod: dict[str, Any]) -> bool:
    phase = str(pod.get("phase") or "").lower()
    if phase == "succeeded":
        return False
    if phase != "running":
        return True
    containers = pod.get("containers")
    if not isinstance(containers, list):
        return False
    for container in containers:
        if not isinstance(container, dict):
            continue
        if container.get("ready") is False:
            return True
        try:
            if int(container.get("restart_count") or container.get("restartCount") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _is_completed_pod(pod: dict[str, Any]) -> bool:
    return str(pod.get("phase") or "").lower() == "succeeded"


def _labels_match_selector(labels: Any, selector: Any) -> bool:
    if not isinstance(labels, dict) or not isinstance(selector, dict) or not selector:
        return False
    return all(str(labels.get(key)) == str(value) for key, value in selector.items())


def _deployment_selector(deployment: dict[str, Any]) -> dict[str, Any]:
    for key in ("selector", "match_labels", "matchLabels"):
        value = deployment.get(key)
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    spec = deployment.get("spec")
    if isinstance(spec, dict):
        selector = spec.get("selector")
        if isinstance(selector, dict):
            match_labels = selector.get("matchLabels") or selector.get("match_labels")
            if isinstance(match_labels, dict):
                return {str(k): str(v) for k, v in match_labels.items()}
    return {}


def _pod_labels(pod: dict[str, Any]) -> dict[str, Any]:
    for key in ("labels", "metadata_labels"):
        value = pod.get(key)
        if isinstance(value, dict):
            return value
    metadata = pod.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("labels"), dict):
        return metadata["labels"]
    return {}


def _filter_workload_pods(
    pods: list[Any],
    deployments: list[Any],
    workload: str,
) -> list[dict[str, Any]]:
    workload = workload.strip()
    candidates = [pod for pod in pods if isinstance(pod, dict)]
    if not workload:
        return []

    exact_pod = [pod for pod in candidates if str(pod.get("name") or "") == workload]
    if exact_pod:
        return exact_pod

    deployment = next(
        (
            item
            for item in deployments
            if isinstance(item, dict) and str(item.get("name") or "") == workload
        ),
        None,
    )
    if deployment is not None:
        selector = _deployment_selector(deployment)
        matched = [
            pod for pod in candidates if _labels_match_selector(_pod_labels(pod), selector)
        ]
        if matched:
            return matched

    return [pod for pod in candidates if str(pod.get("name") or "").startswith(f"{workload}-")]


def _select_workload_pod(pods: list[Any], workload: str) -> str | None:
    matched = _filter_workload_pods(pods, [], workload)
    return str(matched[0]["name"]) if matched and matched[0].get("name") else None


def _select_workload_pod_from_deployments(
    pods: list[Any],
    deployments: list[Any],
    workload: str,
) -> str | None:
    matched = _filter_workload_pods(pods, deployments, workload)
    return str(matched[0]["name"]) if matched and matched[0].get("name") else None


def _log_lines_from_payload(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    for key in ("logs", "log", "text", "output"):
        value = data.get(key)
        if isinstance(value, str):
            return value.splitlines()
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _compact_log_payload(
    payload: dict[str, Any],
    *,
    level: str | None,
    contains: str | None,
    exclude: str | None,
    compact: bool,
) -> dict[str, Any]:
    if not compact:
        return payload

    lines = _log_lines_from_payload(payload)
    if not lines:
        return payload

    include_pattern = contains.lower().strip() if contains else ""
    exclude_pattern = exclude.lower().strip() if exclude else ""
    level_pattern = level.lower().strip() if level else ""
    noisy_patterns = ("httpcore.", "httpx", "sse_starlette.sse", "raw response")
    important_patterns = (
        "error",
        "warning",
        "traceback",
        "exception",
        "timeout",
        "failed",
        " 4",
        " 5",
    )

    selected: list[str] = []
    skipped_debug = 0
    for line in lines:
        lowered = line.lower()
        if include_pattern and include_pattern not in lowered:
            continue
        if exclude_pattern and exclude_pattern in lowered:
            continue
        if level_pattern and level_pattern not in lowered:
            continue
        if any(pattern in lowered for pattern in noisy_patterns) and not any(
            pattern in lowered for pattern in important_patterns
        ):
            skipped_debug += 1
            continue
        selected.append(line)

    highlighted = [
        line for line in selected if any(pattern in line.lower() for pattern in important_patterns)
    ]
    compact_data = dict(payload.get("data") if isinstance(payload.get("data"), dict) else {})
    compact_data.update(
        {
            "lines": selected[-120:],
            "highlighted": highlighted[-40:],
            "line_count": len(lines),
            "returned_line_count": min(len(selected), 120),
            "skipped_debug_lines": skipped_debug,
            "compact": True,
        }
    )
    return {**payload, "data": compact_data}


def _resolve_job_workspace_id(
    workspace_id: str | None,
    *,
    token_workspace_id: str | None = None,
    default_workspace_id: str | None = None,
) -> str:
    explicit_workspace_id = workspace_id.strip() if workspace_id is not None else ""
    token_workspace_id_normalized = (
        token_workspace_id.strip() if token_workspace_id is not None else ""
    )

    if explicit_workspace_id:
        if token_workspace_id_normalized and explicit_workspace_id != token_workspace_id_normalized:
            raise ValueError(
                "workspace_scope_mismatch: explicit workspace_id does not match "
                "token workspace scope"
            )
        return explicit_workspace_id

    if token_workspace_id_normalized:
        return token_workspace_id_normalized

    if default_workspace_id is not None and default_workspace_id.strip():
        return default_workspace_id.strip()

    raise ValueError(
        "workspace_id is required for async job orchestration. "
        "Pass workspace_id or configure MCP_DEFAULT_WORKSPACE_ID."
    )


def _platform_slack_mode_enabled(settings: Settings) -> bool:
    return bool(settings.platform_api_base_url and settings.platform_api_internal_api_key)


def _resolve_slack_tool_access(
    settings: Settings,
    *,
    workspace_id: str | None,
    token_workspace_id: str,
) -> tuple[str | None, PlatformSlackClient | None]:
    resolved_workspace_id = _resolve_job_workspace_id(
        workspace_id,
        token_workspace_id=token_workspace_id,
        default_workspace_id=settings.mcp_default_workspace_id,
    )

    if _platform_slack_mode_enabled(settings):
        return None, PlatformSlackClient(settings, workspace_id=resolved_workspace_id)

    if settings.environment == "production":
        raise ValueError(
            "slack_platform_mode_required: configure PLATFORM_API_BASE_URL and "
            "PLATFORM_API_INTERNAL_TOKEN for Slack MCP tools in production"
        )

    token = settings.slack_bot_token
    if token is None:
        raise ValueError(
            "slack_token_missing: configure platform Slack mode or local SLACK_BOT_TOKEN"
        )

    return token.get_secret_value(), None


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


def _incident_is_active(incident: dict[str, Any]) -> bool:
    status = str(incident.get("status") or "").lower()
    return status not in {"resolved", "completed", "postmortem", "closed"}


def _compact_provider_status(provider_status: dict[str, Any]) -> dict[str, Any]:
    incidents_raw = provider_status.get("incidents")
    incidents_list = incidents_raw if isinstance(incidents_raw, list) else []
    compact_incidents = [_compact_incident(item) for item in incidents_list[:20]]
    active_incidents = [
        incident for incident in compact_incidents if _incident_is_active(incident)
    ]
    historical_incidents = [
        incident for incident in compact_incidents if not _incident_is_active(incident)
    ]

    degraded_raw = provider_status.get("degraded_components")
    degraded_list = degraded_raw if isinstance(degraded_raw, list) else []
    compact_degraded = [_compact_degraded_component(item) for item in degraded_list[:20]]

    compact: dict[str, Any] = {
        "provider": provider_status.get("provider"),
        "status": "ok",
        "indicator": provider_status.get("indicator"),
        "description": provider_status.get("description"),
        "active_incidents": active_incidents,
        "historical_incidents": historical_incidents,
        "degraded_components": compact_degraded,
        "fetched_at": provider_status.get("fetched_at"),
        "truncated": len(incidents_list) > 20,
    }

    if "regional_status" in provider_status:
        compact["regional_status"] = provider_status.get("regional_status") or {}
    if "regional_status_errors" in provider_status:
        compact["regional_status_errors"] = provider_status.get("regional_status_errors") or {}

    return compact


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
        compact_statuses.append(_compact_provider_status(provider_status))

    errors = result.get("errors")
    errors_list = errors if isinstance(errors, list) else []
    checked_at = None
    for provider_status in compact_statuses:
        fetched_at = provider_status.get("fetched_at")
        if isinstance(fetched_at, str) and (checked_at is None or fetched_at > checked_at):
            checked_at = fetched_at

    status = "ok"
    if errors_list and compact_statuses:
        status = "partial"
    elif errors_list and not compact_statuses:
        status = "error"
    elif str(result.get("status") or "").lower() not in {"success", "ok"}:
        status = str(result.get("status") or "unknown")

    provider_names = {
        str(provider_status.get("provider") or "").lower()
        for provider_status in compact_statuses
        if provider_status.get("provider")
    }
    for error in errors_list:
        if not isinstance(error, dict):
            continue
        provider = str(error.get("provider") or "").lower()
        if not provider or provider in provider_names:
            continue
        compact_statuses.append(
            {
                "provider": provider,
                "status": "error",
                "error": error.get("message") or error.get("error") or "provider failed",
                "error_type": error.get("error_type"),
                "active_incidents": [],
                "historical_incidents": [],
                "degraded_components": [],
            }
        )
        provider_names.add(provider)

    compact_result = {
        "status": status,
        "checked_at": checked_at,
        "providers": compact_statuses,
    }
    if errors_list:
        compact_result["errors"] = errors_list
    if "persistence" in result:
        compact_result["persistence"] = result.get("persistence")
    if "provenance" in result:
        compact_result["provenance"] = result.get("provenance")
    return compact_result


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
        if response_mode == "compact" and status == "succeeded":
            return json.dumps(normalized_result, indent=2)
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
    token_workspace_id: str | None = None,
) -> str:
    resolved_token_workspace_id = token_workspace_id or _current_token_workspace_id()
    resolved_workspace_id = _resolve_job_workspace_id(
        workspace_id,
        token_workspace_id=resolved_token_workspace_id,
        default_workspace_id=settings.mcp_default_workspace_id,
    )
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
            "workspace_id": resolved_workspace_id,
            "incident_id": "external-status",
            "payload": {
                "providers": selected_providers,
                "external_status_only": True,
                "days_back": days_back,
                "persist_to_oms": settings.mcp_oms_persist_enabled,
            },
            "artifact_refs": [],
            "evidence_refs": [],
        }
    )

    job_id = submitted["job_id"]
    logger.info(
        "mcp_async_job_submitted tool=external_status_check job_id=%s workspace_id=%s",
        job_id,
        resolved_workspace_id,
    )

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

    @mcp.tool(**_tool_metadata(_specs["incident_summary"]))
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

        resolved_workspace_id = _resolve_job_workspace_id(
            workspace_id,
            token_workspace_id=_current_token_workspace_id(),
            default_workspace_id=settings.mcp_default_workspace_id,
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
        return _build_async_result(
            job_id=submitted["job_id"],
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["correlate_alerts"]))
    async def correlate_alerts(
        alerts: Annotated[
            list[Alert],
            Field(
                min_length=1,
                max_length=500,
                description=(
                    "Alert objects to correlate. Each alert requires alert_id, name, service, "
                    "severity, status, and fired_at; labels may include env, namespace, "
                    "pod, deployment, or other routing context."
                ),
            ),
        ],
        window_minutes: int = 60,
        min_cluster_size: int = 2,
        execution_mode: str = "auto",
        workspace_id: str | None = None,
    ) -> str:
        input_data = CorrelateAlertsInput(
            alerts=alerts,
            window_minutes=window_minutes,
            min_cluster_size=min_cluster_size,
        )
        _resolve_correlation_mode(execution_mode)
        _ = workspace_id
        result: CorrelateAlertsOutput = _correlate_alerts_impl(input_data)
        return result.model_dump_json(indent=2)

    @mcp.tool(**_tool_metadata(_specs["external_status_check"]))
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

    @mcp.tool(**_tool_metadata(_specs["slack_alerts_list"]))
    async def slack_alerts_list(
        channel: str | None = None,
        limit: int | None = None,
        include_raw: bool = False,
        include_threads: bool = False,
        thread_mode: str = "none",
        max_thread_replies: int = 20,
        include_system_messages: bool = False,
        workspace_id: str | None = None,
    ) -> str:
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _workspace_context_required_error()

        try:
            token, platform_client = _resolve_slack_tool_access(
                settings,
                workspace_id=workspace_id,
                token_workspace_id=token_workspace_id,
            )

            selected_channel = (channel or settings.slack_alerts_channel).strip() or "alerts"
            selected_limit = limit or settings.slack_alerts_default_limit
            if selected_limit < 1 or selected_limit > 200:
                raise ValueError("limit must be between 1 and 200")
            selected_thread_mode = _normalize_slack_thread_mode(thread_mode)
            if max_thread_replies < 0 or max_thread_replies > 200:
                raise ValueError("max_thread_replies must be between 0 and 200")

            result = await fetch_slack_alerts(
                token=token,
                channel=selected_channel,
                limit=selected_limit,
                include_raw=include_raw,
                include_threads=include_threads,
                thread_mode=selected_thread_mode,  # type: ignore[arg-type]
                max_thread_replies=max_thread_replies,
                include_system_messages=include_system_messages,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return _platform_slack_error_json(exc)
        return result.model_dump_json(indent=2)

    @mcp.tool(**_tool_metadata(_specs["slack_alert_thread_get"]))
    async def slack_alert_thread_get(
        channel_id: str,
        message_ts: str,
        include_root: bool = True,
        max_replies: int = 50,
        workspace_id: str | None = None,
    ) -> str:
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _workspace_context_required_error()

        try:
            token, platform_client = _resolve_slack_tool_access(
                settings,
                workspace_id=workspace_id,
                token_workspace_id=token_workspace_id,
            )
            if max_replies < 0 or max_replies > 200:
                raise ValueError("max_replies must be between 0 and 200")

            result = await fetch_slack_alert_thread(
                token=token,
                channel_id=channel_id,
                message_ts=message_ts,
                include_root=include_root,
                max_replies=max_replies,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return _platform_slack_error_json(exc)
        return result.model_dump_json(indent=2)

    @mcp.tool(**_tool_metadata(_specs["incident_thread_summary"]))
    async def incident_thread_summary(
        channel_id: str,
        thread_ts: str,
        alert_context: IncidentThreadAlertContext | None = None,
        workspace_id: str | None = None,
    ) -> str:
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _workspace_context_required_error()

        try:
            token, platform_client = _resolve_slack_tool_access(
                settings,
                workspace_id=workspace_id,
                token_workspace_id=token_workspace_id,
            )

            result = await summarize_incident_thread(
                token=token,
                channel_id=channel_id,
                thread_ts=thread_ts,
                alert_context=alert_context.model_dump(exclude_none=True)
                if alert_context
                else None,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return _platform_slack_error_json(exc)
        return json.dumps(result, indent=2)

    @mcp.tool(**_tool_metadata(_specs["k8s_agent_command"]))
    async def k8s_agent_command(
        action: K8sReadOnlyAction,
        params: K8sAgentCommandParams | None = None,
        cluster_id: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action=action,
            params=params.model_dump(exclude_none=True) if params else None,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_connection_health"]))
    async def k8s_connection_health(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        client = PlatformAPIAgentCommandsClient(settings)
        return _json(
            await _k8s_connection_health_payload(
                client=client,
                bearer_token=_current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                timeout_seconds=timeout_seconds,
            )
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_cluster_overview"]))
    async def k8s_cluster_overview(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = _current_bearer_token()
        clusters = await client.list_clusters(bearer_token=bearer_token)
        cluster = _select_k8s_cluster_summary(
            clusters,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            connected_only=True,
        )
        if cluster is None:
            return _json(
                {
                    "status": "offline",
                    "agent_online": False,
                    "error": _NO_CONNECTED_CLUSTER_MESSAGE,
                    "checked_at": _checked_at(),
                }
            )
        overview = await _k8s_cluster_overview_payload(
            client=client,
            bearer_token=bearer_token,
            cluster_id=str(cluster["cluster_id"]),
            timeout_seconds=timeout_seconds,
        )
        overview.update(
            {
                "status": "connected",
                "cluster_id": cluster.get("cluster_id"),
                "cluster_name": cluster.get("name"),
            }
        )
        return _json(overview)

    @mcp.tool(**_tool_metadata(_specs["k8s_namespace_overview"]))
    async def k8s_namespace_overview(
        namespace: str,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = _current_bearer_token()
        resolved_cluster_id = await _resolve_k8s_cluster_id(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
        )
        overview = await _k8s_cluster_overview_payload(
            client=client,
            bearer_token=bearer_token,
            cluster_id=resolved_cluster_id,
            namespace=namespace,
            timeout_seconds=timeout_seconds,
        )
        overview.update({"status": "connected", "cluster_id": resolved_cluster_id})
        return _json(overview)

    @mcp.tool(**_tool_metadata(_specs["k8s_rbac_check"]))
    async def k8s_rbac_check(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        client = PlatformAPIAgentCommandsClient(settings)
        bearer_token = _current_bearer_token()
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
        return _json(payload)

    @mcp.tool(**_tool_metadata(_specs["k8s_agent_status"]))
    async def k8s_agent_status(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        _ = timeout_seconds
        client = PlatformAPIAgentCommandsClient(settings)
        return _json(
            await _k8s_agent_status_payload(
                client=client,
                bearer_token=_current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
            )
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_namespaces"]))
    async def k8s_list_namespaces(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_namespaces",
            params={},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_pods"]))
    async def k8s_list_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_pods",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_get_pod"]))
    async def k8s_get_pod(
        namespace: str,
        pod: str,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_pod",
            params={"namespace": namespace, "pod": pod},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_get_pod_logs"]))
    async def k8s_get_pod_logs(
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: int = 200,
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
    ) -> str:
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
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
        raw = await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_pod_logs",
            params=params,
            timeout_seconds=timeout_seconds,
        )
        payload = json.loads(raw)
        return json.dumps(
            _compact_log_payload(
                payload,
                level=level,
                contains=contains,
                exclude=exclude,
                compact=compact,
            ),
            indent=2,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_events"]))
    async def k8s_list_events(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_events",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_deployments"]))
    async def k8s_list_deployments(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_deployments",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_services"]))
    async def k8s_list_services(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_services",
            params={"namespace": namespace} if namespace else {},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_get_rollout_status"]))
    async def k8s_get_rollout_status(
        namespace: str,
        deployment: str,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        return await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_rollout_status",
            params={"namespace": namespace, "deployment": deployment},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_show_namespaces"]))
    async def k8s_show_namespaces(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await k8s_list_namespaces(
            environment=environment,
            cluster_name=cluster_name,
            cluster_id=cluster_id,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_show_pods"]))
    async def k8s_show_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        return await k8s_list_pods(
            namespace=namespace,
            environment=environment,
            cluster_name=cluster_name,
            cluster_id=cluster_id,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_show_unhealthy_pods"]))
    async def k8s_show_unhealthy_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        result = await _dispatch_k8s_pods_for_analysis(
            settings=settings,
            namespace=namespace,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            timeout_seconds=timeout_seconds,
        )
        data = result.get("data") if isinstance(result, dict) else None
        pods = data.get("pods") if isinstance(data, dict) else []
        pods = pods if isinstance(pods, list) else []
        unhealthy = [pod for pod in pods if isinstance(pod, dict) and _is_unhealthy_pod(pod)]
        completed = [pod for pod in pods if isinstance(pod, dict) and _is_completed_pod(pod)]
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "pods": unhealthy,
                    "count": len(unhealthy),
                    "completed_jobs": completed,
                    "completed_count": len(completed),
                },
                "error": None,
            },
            indent=2,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_analyze_workload"]))
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
        tail_lines: int = 100,
        timeout_seconds: int = 30,
    ) -> str:
        if not namespace:
            raise ValueError(_MISSING_NAMESPACE_MESSAGE)
        if not workload:
            raise ValueError("Please specify a pod or deployment name to analyze.")

        rollout = await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.get_rollout_status",
            params={"namespace": namespace, "deployment": workload},
            timeout_seconds=timeout_seconds,
        )
        pods = await _dispatch_k8s_pods_for_analysis(
            settings=settings,
            namespace=namespace,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            timeout_seconds=timeout_seconds,
        )
        pods_data = pods.get("data") if isinstance(pods, dict) else None
        pod_items = pods_data.get("pods") if isinstance(pods_data, dict) else []
        deployments = await _dispatch_k8s_agent_command(
            settings=settings,
            cluster_id=cluster_id,
            environment=environment,
            cluster_name=cluster_name,
            action="k8s.list_deployments",
            params={"namespace": namespace},
            timeout_seconds=timeout_seconds,
        )
        deployments_data = json.loads(deployments).get("data")
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
        logs_data = None
        if selected_pod:
            logs = await _dispatch_k8s_agent_command(
                settings=settings,
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
            )
            logs_data = json.loads(logs).get("data")
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "rollout_status": json.loads(rollout).get("data"),
                    "pods": related_pods,
                    "pods_total": len(related_pods),
                    "logs": logs_data,
                    "selected_pod": selected_pod,
                    "hint": (
                        "No matching pod was found for logs."
                        if selected_pod is None
                        else "Logs are from the selected pod."
                    ),
                    "tail_lines": tail_lines,
                },
                "error": None,
            },
            indent=2,
        )

    register_resources(mcp)

    return mcp
