"""Kubernetes command orchestration helpers."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from incidentflow_mcp.mcp.services import kubernetes_analysis as analysis
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient

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

_top_restarts = analysis._top_restarts
_pod_brief = analysis._pod_brief
_warning_events = analysis._warning_events
_warning_event_summary = analysis._warning_event_summary
_is_completed_pod = analysis._is_completed_pod
_is_unhealthy_pod = analysis._is_unhealthy_pod


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


def _checked_at() -> str:
    return datetime.now(tz=UTC).isoformat()


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


def _k8s_failed_response(
    *,
    code: str,
    message: str,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "status": "failed",
        "summary": summary or message,
        "health": "unknown",
        "severity": "warning",
        "findings": [message],
        "recommendations": ["Verify namespace and workload name"],
        "data": details or {},
        "error": error,
    }


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


async def _send_k8s_command(
    *,
    client: PlatformAPIAgentCommandsClient,
    bearer_token: str,
    cluster_id: str,
    action: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    try:
        return await client.send_agent_command(
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
    _health_start = time.perf_counter()
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

    agent_lookup_ms = round((time.perf_counter() - _health_start) * 1000, 2)
    started = time.perf_counter()
    namespaces_response = await _send_k8s_command(
        client=client,
        bearer_token=bearer_token,
        cluster_id=str(resolved_cluster_id),
        action="k8s.list_namespaces",
        params={},
        timeout_seconds=timeout_seconds,
    )
    list_namespaces_ms = round((time.perf_counter() - started) * 1000, 2)
    latency_ms = list_namespaces_ms
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
            response = await _send_k8s_command(
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
            response = await _send_k8s_command(
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

    total_health_ms = round((time.perf_counter() - _health_start) * 1000, 2)
    latency_interpretation = (
        "latency_ms reflects the platform→gateway→k8s-agent roundtrip for "
        "k8s.list_namespaces. MCP HTTP handler latency is logged separately "
        "by the observability middleware (see duration_ms in http_request logs)."
    )
    status.update(
        {
            "status": "connected" if _command_ok(namespaces_response) else "degraded",
            "latency_ms": latency_ms,
            "latency_breakdown": {
                "agent_lookup_ms": agent_lookup_ms,
                "list_namespaces_ms": list_namespaces_ms,
                "total_health_check_ms": total_health_ms,
                "mcp_handler_ms": None,  # measured by HTTP middleware, not available here
            },
            "latency_interpretation": latency_interpretation,
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
    warning_summary = _warning_event_summary(events, pods)
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
        "warning_event_summary": warning_summary,
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
    namespaces_response = await _send_k8s_command(
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
    semaphore = asyncio.Semaphore(5)

    async def _namespace_overview_commands(
        item: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with semaphore:
            (
                pods_response,
                deployments_response,
                services_response,
                events_response,
            ) = await asyncio.gather(
                _send_k8s_command(
                    client=client,
                    bearer_token=bearer_token,
                    cluster_id=cluster_id,
                    action="k8s.list_pods",
                    params={"namespace": item},
                    timeout_seconds=timeout_seconds,
                ),
                _send_k8s_command(
                    client=client,
                    bearer_token=bearer_token,
                    cluster_id=cluster_id,
                    action="k8s.list_deployments",
                    params={"namespace": item},
                    timeout_seconds=timeout_seconds,
                ),
                _send_k8s_command(
                    client=client,
                    bearer_token=bearer_token,
                    cluster_id=cluster_id,
                    action="k8s.list_services",
                    params={"namespace": item},
                    timeout_seconds=timeout_seconds,
                ),
                _send_k8s_command(
                    client=client,
                    bearer_token=bearer_token,
                    cluster_id=cluster_id,
                    action="k8s.list_events",
                    params={"namespace": item},
                    timeout_seconds=timeout_seconds,
                ),
            )
            return pods_response, deployments_response, services_response, events_response

    namespace_responses = await asyncio.gather(
        *(_namespace_overview_commands(item) for item in namespaces)
    )
    for (
        pods_response,
        deployments_response,
        services_response,
        events_response,
    ) in namespace_responses:
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
        response = await _send_k8s_command(
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
        response = await _send_k8s_command(
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
