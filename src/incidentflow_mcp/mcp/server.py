"""
MCP server definition.

Uses FastMCP (official MCP Python SDK) with Streamable HTTP transport.
All tools are registered here and wired to their implementation modules.
"""

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import UrlElicitationRequiredError
from pydantic import BaseModel, Field, ValidationError

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.auth.principal import IncidentFlowPrincipal, require_principal
from incidentflow_mcp.config import Settings, get_settings
from incidentflow_mcp.integrations import (
    IntegrationStatusService,
    ResolvedIntegrationContext,
    attach_integration_context,
    integration_actions,
    resolve_tool_integration_context,
)
from incidentflow_mcp.mcp.resources import register_resources
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.platform_api.argocd_client import PlatformArgoCDClient
from incidentflow_mcp.platform_api.grafana_client import PlatformGrafanaClient
from incidentflow_mcp.platform_api.slack_client import PlatformSlackAPIError, PlatformSlackClient
from incidentflow_mcp.tools import argocd as _argocd_tools
from incidentflow_mcp.tools import grafana as _grafana_tools
from incidentflow_mcp.tools.contracts import apply_tool_contract
from incidentflow_mcp.tools.correlate_alerts import correlate_alerts as _correlate_alerts_impl
from incidentflow_mcp.tools.incident_summary import incident_summary as _incident_summary_impl
from incidentflow_mcp.tools.registry import build_tool_description, get_tool_specs
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
    "k8s.describe_pod",
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
    metadata = {
        "name": spec.name,
        "title": spec.title,
        "description": build_tool_description(
            spec,
            environment=get_settings().runtime_environment(),
        ),
        "annotations": spec.annotations,
    }
    if getattr(spec, "meta", None):
        metadata["meta"] = spec.meta
    if getattr(spec, "structured_output", None) is not None:
        metadata["structured_output"] = spec.structured_output
    return metadata


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


def _normalize_correlation_alerts(
    alerts: list[Alert] | list[dict[str, Any]] | None,
    alerts_json: str | None,
) -> list[Alert]:
    payload: Any = alerts
    if payload is None and alerts_json:
        try:
            payload = json.loads(alerts_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"alerts_json must be valid JSON: {exc.msg}") from exc

    if payload is None:
        raise ValueError("Either alerts or legacy alerts_json must be provided")
    if not isinstance(payload, list):
        raise ValueError("alerts must be a list of alert objects")

    return [item if isinstance(item, Alert) else Alert.model_validate(item) for item in payload]


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


def _current_principal(settings: Settings) -> IncidentFlowPrincipal:
    return require_principal(get_current_auth_context(), settings=settings)


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


async def _send_k8s_agent_command(
    *,
    settings: Settings,
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
    bearer_token = _current_bearer_token()
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
    namespace: str | None,
    cluster_id: str | None,
    environment: str | None,
    cluster_name: str | None,
    timeout_seconds: int,
    integration_context: ResolvedIntegrationContext | None = None,
) -> dict[str, Any]:
    raw = await _send_k8s_agent_command(
        settings=settings,
        cluster_id=cluster_id,
        environment=environment,
        cluster_name=cluster_name,
        action="k8s.list_pods",
        params={"namespace": namespace} if namespace else {},
        timeout_seconds=timeout_seconds,
        integration_context=integration_context,
    )
    return json.loads(raw)


def _checked_at() -> str:
    return datetime.now(tz=UTC).isoformat()


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


def _structured_guard_error(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "status": "failed", "error": raw}
    return decoded if isinstance(decoded, dict) else {"ok": False, "status": "failed", "error": raw}


def _structured_tool_exception(exc: Exception, *, code: str = "TOOL_ERROR") -> dict[str, Any]:
    """Return a stable MCP tool error envelope for exceptions from upstream clients."""
    error: dict[str, Any] = {
        "code": code,
        "message": str(exc),
    }
    if isinstance(exc, ValidationError):
        error["code"] = "VALIDATION_ERROR"
        error["details"] = exc.errors()
    if isinstance(exc, httpx.HTTPStatusError):
        error["code"] = f"HTTP_{exc.response.status_code}"
        error["http_status"] = exc.response.status_code
        try:
            body = exc.response.json()
        except ValueError:
            body = exc.response.text
        if body:
            error["upstream_response"] = body
    return {"ok": False, "status": "failed", "warnings": [], "error": error}


async def _run_tool_with_structured_errors(
    tool: Any,
    arguments: dict[str, Any],
    context: Any | None = None,
    convert_result: bool = False,
) -> Any:
    """Run a FastMCP tool without converting validation/runtime errors to text-only ToolError."""
    try:
        result = await tool.fn_metadata.call_fn_with_arg_validation(
            tool.fn,
            tool.is_async,
            arguments,
            {tool.context_kwarg: context} if tool.context_kwarg is not None else None,
        )
        if convert_result:
            result = tool.fn_metadata.convert_result(result)
        return apply_tool_contract(result, tool_name=tool.name)
    except UrlElicitationRequiredError:
        raise
    except Exception as exc:
        return apply_tool_contract(_structured_tool_exception(exc), tool_name=tool.name)


def _harden_fastmcp_tool_contracts(mcp: FastMCP) -> None:
    """Make FastMCP argument validation strict and keep validation errors structured."""
    for tool in mcp._tool_manager.list_tools():
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)
        tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
        object.__setattr__(
            tool,
            "run",
            MethodType(_run_tool_with_structured_errors, tool),
        )


_CAPABILITIES_TOOL_NAME = "incidentflow_capabilities"
_VERSION_TOOL_NAME = "mcp_version"
_AUTH_STATUS_TOOL_NAME = "incidentflow_auth_status"
_INTEGRATIONS_STATUS_TOOL_NAME = "incidentflow_integrations_status"
_META_TOOL_NAMES = {
    _CAPABILITIES_TOOL_NAME,
    _VERSION_TOOL_NAME,
    _AUTH_STATUS_TOOL_NAME,
    _INTEGRATIONS_STATUS_TOOL_NAME,
}
_SERVER_DESCRIPTION = "HTTP-based MCP server for IncidentFlow AI-powered incident management."
_CAPABILITY_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "kubernetes",
        "Kubernetes",
        (
            "k8s_agent_status",
            "k8s_connection_health",
            "k8s_cluster_overview",
            "k8s_namespace_overview",
            "k8s_list_namespaces",
            "k8s_list_pods",
            "k8s_show_unhealthy_pods",
            "k8s_get_pod",
            "k8s_describe_pod",
            "k8s_debug_pod",
            "k8s_get_pod_logs",
            "k8s_list_events",
            "k8s_list_deployments",
            "k8s_get_rollout_status",
            "k8s_analyze_workload",
            "k8s_list_services",
            "k8s_rbac_check",
        ),
    ),
    (
        "argocd",
        "Argo CD",
        (
            "argocd_connection_health",
            "argocd_list_applications",
            "argocd_get_application",
            "argocd_get_application_resources",
            "argocd_get_sync_history",
            "argocd_get_last_operation",
            "argocd_find_recent_deployments",
            "argocd_analyze_application",
        ),
    ),
    (
        "grafana_prometheus",
        "Grafana / Prometheus",
        (
            "grafana_list_dashboards",
            "grafana_get_dashboard",
            "grafana_extract_panel_queries",
            "grafana_metrics_query",
            "grafana_metrics_query_range",
            "analyze_dashboard_health",
            "grafana_get_panel_view",
        ),
    ),
    (
        "slack_incidents",
        "Slack / Incidents",
        (
            "slack_alerts_list",
            "slack_alert_thread_get",
            "incident_thread_summary",
            "incident_summary",
            "correlate_alerts",
            "external_status_check",
        ),
    ),
    (
        "knowledge",
        "Knowledge",
        (
            "public_knowledge_search",
            "private_knowledge_search",
            "knowledge_get",
            "knowledge_upsert",
        ),
    ),
)


def _capability_tool_entry(spec: Any, *, response_mode: str) -> dict[str, Any]:
    read_only = bool(spec.annotations.get("readOnlyHint"))
    entry = {
        "canonical_name": spec.name,
        "title": spec.title,
        "required_integration": getattr(spec, "required_integration", None),
        "supports_shared_dev_fallback": bool(getattr(spec, "supports_shared_dev_fallback", False)),
        "read_only": read_only,
        "write_memory_only": spec.name == "knowledge_upsert",
    }
    if response_mode == "full":
        entry["description"] = spec.description
        entry["annotations"] = {
            "readOnlyHint": read_only,
            "openWorldHint": bool(spec.annotations.get("openWorldHint")),
            "destructiveHint": bool(spec.annotations.get("destructiveHint")),
        }
    return entry


def _incidentflow_capabilities_payload(
    *, response_mode: str = "compact", category: str | None = None
) -> dict[str, Any]:
    resolved_response_mode = response_mode if response_mode in {"compact", "full"} else "compact"
    specs_by_name = {spec.name: spec for spec in get_tool_specs()}
    operational_specs = {
        name: spec for name, spec in specs_by_name.items() if name not in _META_TOOL_NAMES
    }

    categories = []
    categorized_names: set[str] = set()
    for category_id, label, names in _CAPABILITY_CATEGORIES:
        if category and category != category_id:
            categorized_names.update(names)
            continue
        tools = [
            _capability_tool_entry(operational_specs[name], response_mode=resolved_response_mode)
            for name in names
        ]
        categorized_names.update(names)
        categories.append(
            {
                "id": category_id,
                "label": label,
                "total": len(tools),
                "tools": tools,
            }
        )

    uncategorized = sorted(set(operational_specs) - categorized_names)
    if uncategorized and category in (None, "uncategorized"):
        categories.append(
            {
                "id": "uncategorized",
                "label": "Uncategorized",
                "total": len(uncategorized),
                "tools": [
                    _capability_tool_entry(
                        operational_specs[name], response_mode=resolved_response_mode
                    )
                    for name in uncategorized
                ],
            }
        )

    read_only_count = sum(
        1 for spec in operational_specs.values() if spec.annotations.get("readOnlyHint") is True
    )
    write_memory_only_count = sum(1 for name in operational_specs if name == "knowledge_upsert")
    return {
        "name": "incidentflow",
        "source": "incidentflow-mcp",
        "summary": (
            "This inventory is canonical for this IncidentFlow MCP server. Use these "
            "canonical_name values and categories as the authoritative runtime tool list."
        ),
        "total": len(operational_specs),
        "read_only": read_only_count,
        "write_memory_only": write_memory_only_count,
        "response_mode": resolved_response_mode,
        "category_filter": category,
        "categories": categories,
        "notes": [
            "This inventory is generated from the MCP tool registry.",
            (
                "Use this inventory instead of cached docs, stale submission metadata, "
                "or search-ranked discovery when a complete tool list is needed."
            ),
            "IncidentFlow meta-tools are excluded from total and categories.",
        ],
        "checked_at": _checked_at(),
    }


def _normalize_build_version(raw: str | None, fallback: str) -> str:
    version = (raw or "").strip() or fallback
    if version.startswith("dev-v"):
        return version.removeprefix("dev-v")
    if version.startswith("v"):
        return version.removeprefix("v")
    return version


def _environment_from_build_metadata(
    *,
    tag: str | None,
    build_environment: str | None,
    fallback: str,
) -> str:
    explicit_environment = (build_environment or "").strip()
    if explicit_environment:
        return explicit_environment

    normalized = (tag or "").strip().lower()
    if normalized.startswith("dev-"):
        return "dev"
    if normalized.startswith("v"):
        return "prod"
    return fallback


def _mcp_version_payload(settings: Settings) -> dict[str, Any]:
    specs = get_tool_specs()
    meta_count = sum(1 for spec in specs if spec.name in _META_TOOL_NAMES)
    tag = (settings.mcp_build_tag or "").strip() or None
    version_source = settings.mcp_build_version or tag
    return {
        "service": settings.mcp_build_service or settings.mcp_server_name,
        "version": _normalize_build_version(version_source, settings.mcp_server_version),
        "tag": tag,
        "commit": (settings.mcp_build_commit or "").strip() or None,
        "built_at": (settings.mcp_build_built_at or "").strip() or None,
        "environment": _environment_from_build_metadata(
            tag=tag,
            build_environment=settings.mcp_build_environment,
            fallback=settings.environment,
        ),
        "tools": {
            "registered": len(specs),
            "operational": len(specs) - meta_count,
            "meta": meta_count,
        },
        "image": {
            "ref": (settings.mcp_image_ref or "").strip() or None,
            "digest": (settings.mcp_image_digest or "").strip() or None,
            "signed": settings.mcp_image_signed,
            "signature_verified": settings.mcp_image_signature_verified,
            "signature_issuer": (settings.mcp_image_signature_issuer or "").strip() or None,
            "signature_identity": (settings.mcp_image_signature_identity or "").strip() or None,
        },
        "description": _SERVER_DESCRIPTION,
    }


def _client_payload() -> dict[str, str]:
    auth_context = get_current_auth_context() or {}
    client_id = str(auth_context.get("client_id") or "").strip()
    client_name = client_id or "MCP client"
    lowered = client_name.lower()
    if "claude" in lowered:
        client_name = "Claude Code"
    elif "codex" in lowered:
        client_name = "Codex"
    elif client_id == "oauth-client":
        client_name = "OAuth MCP client"
    elif _looks_like_secret_identifier(client_id):
        client_name = "OAuth MCP client"
    return {"name": client_name, "type": "mcp"}


def _looks_like_secret_identifier(value: str) -> bool:
    lowered = value.lower().strip()
    if lowered.startswith(("if_oac_", "if_pat_", "sk-", "xoxb-", "xoxp-")):
        return True
    return bool(re.search(r"(access[_-]?token|refresh[_-]?token|api[_-]?key|secret)", lowered))


def _principal_permissions(principal: IncidentFlowPrincipal) -> list[str]:
    permissions = ["workspace.read", "integrations.read"]
    if principal.workspace.role.lower() in {"owner", "admin"}:
        permissions.append("integrations.manage")
    return permissions


async def _incidentflow_auth_status_payload(
    *,
    settings: Settings,
    principal: IncidentFlowPrincipal,
) -> dict[str, Any]:
    statuses = await IntegrationStatusService(settings).get_statuses(principal)
    connected_integrations = [
        name
        for name, status in statuses.items()
        if status.status == "connected" and status.source == "workspace"
    ]
    available_tool_groups = ["platform"]
    available_tool_groups.extend(
        name for name, status in statuses.items() if status.status == "connected"
    )

    return {
        "authenticated": principal.authenticated,
        "authMethod": principal.auth_method,
        "client": _client_payload(),
        "user": {
            "email": principal.user.email,
        },
        "workspace": {
            "id": principal.workspace.id,
            "slug": principal.workspace.slug,
            "name": principal.workspace.name,
            "role": principal.workspace.role,
        },
        "permissions": _principal_permissions(principal),
        "connectedIntegrations": connected_integrations,
        "availableToolGroups": available_tool_groups,
        "environment": principal.runtime.environment,
    }


async def _incidentflow_integrations_status_payload(
    *,
    settings: Settings,
    principal: IncidentFlowPrincipal,
) -> dict[str, Any]:
    statuses = await IntegrationStatusService(settings).get_statuses(principal)
    payload: dict[str, Any] = {}
    for name, status in statuses.items():
        item = status.public_dict()
        if status.status == "not_connected":
            item["actions"] = integration_actions(name, settings)
        payload[name] = item

    kubernetes = statuses["kubernetes"]
    if kubernetes.source == "shared_dev":
        payload["kubernetes"].update(
            {
                "workspaceIntegration": "not_connected",
                "warning": "Using the shared IncidentFlow development Kubernetes agent.",
                "workspaceActions": integration_actions("kubernetes", settings),
                "effectiveConnection": {
                    "type": "shared_dev_agent",
                    "cluster": settings.shared_dev_kubernetes_cluster_name,
                    "environment": principal.runtime.environment,
                },
            }
        )
    return payload


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


def _container_restart_count(container: dict[str, Any]) -> int:
    try:
        return int(container.get("restart_count") or container.get("restartCount") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_k8s_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_k8s_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _container_last_restart_at(container: dict[str, Any]) -> datetime | None:
    direct = _parse_k8s_timestamp(
        container.get("last_restart_at") or container.get("lastRestartAt")
    )
    if direct is not None:
        return direct
    last_state = container.get("last_state") or container.get("lastState") or {}
    if not isinstance(last_state, dict):
        return None
    terminated = last_state.get("terminated") or {}
    if not isinstance(terminated, dict):
        return None
    return _parse_k8s_timestamp(terminated.get("finished_at") or terminated.get("finishedAt"))


def _container_last_termination(container: dict[str, Any]) -> dict[str, Any] | None:
    last_state = container.get("last_state") or container.get("lastState") or {}
    if not isinstance(last_state, dict):
        return None
    terminated = last_state.get("terminated") or {}
    if not isinstance(terminated, dict) or not terminated:
        return None
    return terminated


def _restart_window_summary(
    containers: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = now or datetime.now(tz=UTC)
    last_restart_at: datetime | None = None
    restarts_last_1h = 0
    restarts_last_24h = 0
    for container in containers:
        restart_count = _container_restart_count(container)
        if restart_count <= 0:
            continue
        restarted_at = _container_last_restart_at(container)
        if restarted_at is None:
            continue
        if last_restart_at is None or restarted_at > last_restart_at:
            last_restart_at = restarted_at
        age = checked_at - restarted_at
        if age.total_seconds() < 0:
            continue
        if age <= timedelta(hours=1):
            restarts_last_1h += restart_count
        if age <= timedelta(hours=24):
            restarts_last_24h += restart_count
    return {
        "last_restart_at": _format_k8s_timestamp(last_restart_at),
        "restarts_last_1h": restarts_last_1h,
        "restarts_last_24h": restarts_last_24h,
    }


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
    containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
    return {
        "namespace": pod.get("namespace"),
        "pod": pod.get("name"),
        "phase": pod.get("phase"),
        "node": pod.get("node_name") or pod.get("nodeName"),
        "restarts": _pod_restart_count(pod),
        **_restart_window_summary(containers),
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


def _parse_k8s_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_ready_pod(pod: dict[str, Any]) -> bool:
    if _is_completed_pod(pod):
        return False
    if str(pod.get("phase") or "").lower() != "running":
        return False
    containers = pod.get("containers")
    if not isinstance(containers, list) or not containers:
        return False
    return all(
        bool(container.get("ready")) for container in containers if isinstance(container, dict)
    )


def _event_pod_name(event: dict[str, Any]) -> str | None:
    involved = event.get("involved_object") or event.get("involvedObject") or event.get("object")
    if isinstance(involved, dict):
        kind = str(involved.get("kind") or "").lower()
        name = str(involved.get("name") or "").strip()
        if kind == "pod" and name:
            return name
    value = event.get("object") or event.get("involved_object_name") or event.get("name")
    if isinstance(value, str):
        match = re.search(r"\bpod/([^\s]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _classify_warning_event(
    event: dict[str, Any],
    *,
    pods_by_name: dict[str, dict[str, Any]],
    now: datetime,
    stale_after_minutes: int = 15,
) -> dict[str, Any]:
    last_seen = (
        event.get("last_seen")
        or event.get("lastSeen")
        or event.get("lastTimestamp")
        or event.get("eventTime")
    )
    parsed_last_seen = _parse_k8s_timestamp(last_seen)
    age_minutes = (
        round((now - parsed_last_seen).total_seconds() / 60, 1)
        if parsed_last_seen is not None
        else None
    )
    pod_name = _event_pod_name(event)
    pod = pods_by_name.get(pod_name or "")
    pod_ready = _is_ready_pod(pod) if pod is not None else False
    stale = age_minutes is not None and age_minutes >= stale_after_minutes
    pod_was_replaced = pod_name is not None and pod is None
    classification = (
        "stale_rollout_warning" if stale and (pod_ready or pod_was_replaced) else "active_warning"
    )

    return {
        **event,
        "pod": pod_name,
        "pod_exists": pod is not None if pod_name else None,
        "pod_ready": pod_ready,
        "age_minutes": age_minutes,
        "classification": classification,
    }


def _warning_event_summary(events: list[Any], pods: list[Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    pods_by_name = {
        str(pod.get("name")): pod for pod in pods if isinstance(pod, dict) and pod.get("name")
    }
    classified = [
        _classify_warning_event(event, pods_by_name=pods_by_name, now=now)
        for event in _warning_events(events, limit=50)
        if isinstance(event, dict)
    ]
    active = [event for event in classified if event["classification"] == "active_warning"]
    stale = [event for event in classified if event["classification"] == "stale_rollout_warning"]
    return {
        "active_warning_events": len(active),
        "stale_rollout_warning_events": len(stale),
        "active_examples": active[:5],
        "stale_examples": stale[:5],
    }


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
    for item in namespaces:
        pods_response = await _send_k8s_command(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_pods",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        deployments_response = await _send_k8s_command(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_deployments",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        services_response = await _send_k8s_command(
            client=client,
            bearer_token=bearer_token,
            cluster_id=cluster_id,
            action="k8s.list_services",
            params={"namespace": item},
            timeout_seconds=timeout_seconds,
        )
        events_response = await _send_k8s_command(
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
            if int(container.get("restart_count") or container.get("restartCount") or 0) > 5:
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


def _strip_image_digest(image: str) -> str:
    """Remove @sha256:... digest from an image reference, keep repo:tag."""
    at = image.find("@")
    return image[:at] if at != -1 else image


def _sanitize_pod(
    pod: dict[str, Any],
    *,
    include_labels: bool = False,
    include_images: bool = True,
    include_node: bool = True,
) -> dict[str, Any]:
    """Allowlist-based pod summary safe for SaaS output.

    Never exposes labels, node internals, image digests, annotations,
    env vars, volumes, serviceAccount, ownerReferences, or containerIDs.
    """
    containers_raw = pod.get("containers") or []
    containers: list[dict[str, Any]] = []
    for c in containers_raw:
        if not isinstance(c, dict):
            continue
        entry: dict[str, Any] = {
            "name": str(c.get("name") or ""),
            "ready": bool(c.get("ready")),
            "restart_count": int(c.get("restart_count") or 0),
        }
        if include_images:
            entry["image"] = _strip_image_digest(str(c.get("image") or ""))
        containers.append(entry)

    all_ready = bool(containers) and all(c["ready"] for c in containers)
    total_restarts = sum(c["restart_count"] for c in containers)

    summary: dict[str, Any] = {
        "name": str(pod.get("name") or ""),
        "namespace": str(pod.get("namespace") or ""),
        "phase": str(pod.get("phase") or ""),
        "ready": all_ready,
        "restarts": total_restarts,
        "age": str(pod.get("age") or ""),
        "containers": containers,
    }
    if include_node:
        summary["node"] = str(pod.get("node_name") or "")
    if include_labels:
        raw_labels = pod.get("labels")
        if isinstance(raw_labels, dict):
            summary["labels"] = raw_labels
    return summary


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
        matched = [pod for pod in candidates if _labels_match_selector(_pod_labels(pod), selector)]
        if matched:
            return matched

    return [pod for pod in candidates if str(pod.get("name") or "").startswith(f"{workload}-")]


def _workload_from_pod_name(pod_name: str) -> str:
    """Derive deployment/workload name by stripping random k8s suffixes.

    incidentflow-mcp-76f5987dc5-j5r6d  ->  incidentflow-mcp
    my-service-6d7f9b-xk2z9            ->  my-service
    standalone-pod                     ->  standalone-pod (unchanged)
    """
    import re as _re

    # ReplicaSet pods: {deployment}-{rs-hash~10}-{pod-hash~5}
    m = _re.match(r"^(.+?)-[a-z0-9]{9,10}-[a-z0-9]{5}$", pod_name)
    if m:
        return m.group(1)
    # DaemonSet / StatefulSet: {name}-{hash5}
    m = _re.match(r"^(.+?)-[a-z0-9]{5}$", pod_name)
    if m:
        return m.group(1)
    return pod_name


def _deduplicate_events(events: list[Any]) -> list[dict[str, Any]]:
    """Collapse repeated events into single entries with occurrence counts."""
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        reason = str(event.get("reason") or "")
        message = str(event.get("message") or "")[:120]
        involved = event.get("involved_object") or event.get("object") or {}
        obj_name = str(involved.get("name") if isinstance(involved, dict) else "")
        namespace = str(event.get("namespace") or "")
        key = (namespace, obj_name, reason, message)
        if key not in groups:
            entry = dict(event)
            entry["count"] = int(event.get("count") or 1)
            groups[key] = entry
        else:
            existing = groups[key]
            existing["count"] = existing.get("count", 1) + int(event.get("count") or 1)
            new_ls = str(event.get("last_seen") or event.get("lastSeen") or "")
            old_ls = str(existing.get("last_seen") or existing.get("lastSeen") or "")
            if new_ls > old_ls:
                existing["last_seen"] = new_ls
    return list(groups.values())


def _sort_events_for_display(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort events: warnings first, then newest first within each group."""

    def _key(e: dict[str, Any]) -> tuple[int, float]:
        type_order = 0 if str(e.get("type") or "").lower() == "warning" else 1
        last_seen = e.get("last_seen") or e.get("lastSeen") or e.get("lastTimestamp") or ""
        ts = _parse_k8s_timestamp(str(last_seen))
        return (type_order, -ts.timestamp() if ts is not None else 0.0)

    return sorted(events, key=_key)


def _events_for_pod(events: list[Any], pod_name: str) -> list[dict[str, Any]]:
    """Filter an event list to events that involve a specific pod."""
    result: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        involved = event.get("involved_object") or event.get("object") or {}
        if isinstance(involved, dict):
            kind = str(involved.get("kind") or "").lower()
            name = str(involved.get("name") or "")
            if kind == "pod" and name == pod_name:
                result.append(event)
                continue
        obj_str = str(event.get("object") or "")
        if f"pod/{pod_name}" in obj_str.lower() or f"Pod/{pod_name}" in obj_str:
            result.append(event)
    return result


def _diagnose_pod(
    pod_raw: dict[str, Any],
    pod_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect common pod failure patterns from pod data and filtered events."""
    phase = str(pod_raw.get("phase") or "").lower()
    containers = [c for c in (pod_raw.get("containers") or []) if isinstance(c, dict)]
    total_restarts = sum(_container_restart_count(c) for c in containers)
    not_ready = [c for c in containers if not c.get("ready")]

    event_reasons: set[str] = set()
    event_messages: list[str] = []
    for e in pod_events:
        if isinstance(e, dict):
            r = str(e.get("reason") or "").lower()
            if r:
                event_reasons.add(r)
            event_messages.append(str(e.get("message") or "").lower())

    issues: list[dict[str, Any]] = []
    recommendations: list[str] = []

    if "backoff" in event_reasons or "crashloopbackoff" in event_reasons:
        issues.append({"type": "CrashLoopBackOff", "severity": "critical"})
        recommendations.append("Run k8s_get_pod_logs to find the crash reason")

    if "imagepullbackoff" in event_reasons or "errimagepull" in event_reasons:
        issues.append({"type": "ImagePullBackOff", "severity": "critical"})
        recommendations.append("Check image name, tag, and registry credentials")
    elif any("pull" in msg for msg in event_messages) and "failed" in event_reasons:
        issues.append({"type": "ImagePullFailure", "severity": "critical"})
        recommendations.append("Check image name, tag, and registry credentials")

    if "oomkilling" in event_reasons or any("oom" in msg for msg in event_messages):
        issues.append({"type": "OOMKilled", "severity": "critical"})
        recommendations.append(
            "Container exceeded memory limit — increase resources.limits.memory or fix memory leak"
        )

    if "failedscheduling" in event_reasons:
        issues.append({"type": "FailedScheduling", "severity": "warning"})
        recommendations.append("Check node resources and pod resource requests")

    readiness_msgs = [msg for msg in event_messages if "readiness" in msg]
    liveness_msgs = [msg for msg in event_messages if "liveness" in msg]
    startup_msgs = [msg for msg in event_messages if "startup" in msg]
    if "unhealthy" in event_reasons:
        current_probe_failure = bool(not_ready) or phase != "running"
        restart_probe_failure = total_restarts > 0
        if readiness_msgs and current_probe_failure:
            issues.append({"type": "ReadinessProbeFailure", "severity": "warning"})
            recommendations.append("Check readiness probe endpoint and application startup time")
        if liveness_msgs and (current_probe_failure or restart_probe_failure):
            issues.append({"type": "LivenessProbeFailure", "severity": "warning"})
            recommendations.append("Check liveness probe — container may be restarting")
        if startup_msgs and current_probe_failure:
            issues.append({"type": "StartupProbeFailure", "severity": "warning"})
            recommendations.append("Startup probe failed — consider increasing initialDelaySeconds")

    if total_restarts > 5 and not any(i["type"] == "CrashLoopBackOff" for i in issues):
        issues.append({"type": "HighRestartCount", "count": total_restarts, "severity": "warning"})
        recommendations.append(
            f"Pod has restarted {total_restarts} times — check logs for past crash reasons"
        )

    if phase == "pending":
        if not any(i["type"] == "FailedScheduling" for i in issues):
            issues.append({"type": "Pending", "severity": "warning"})
            recommendations.append(
                "Pod is waiting — check events for scheduling or image pull issues"
            )
    elif phase == "failed":
        issues.append({"type": "PodFailed", "severity": "critical"})
        recommendations.append("Pod is in Failed state — check logs for exit reason")
    elif phase == "unknown":
        issues.append({"type": "UnknownPhase", "severity": "warning"})
        recommendations.append("Node may be unreachable — check node status")

    if not_ready and not issues and phase == "running":
        issues.append(
            {
                "type": "ContainersNotReady",
                "containers": [c.get("name") for c in not_ready],
                "severity": "warning",
            }
        )
        recommendations.append(
            "Containers are not ready — check readiness probe and application startup"
        )

    historical_warnings: list[str] = []
    if "unhealthy" in event_reasons and not issues and phase == "running" and not not_ready:
        if readiness_msgs:
            historical_warnings.append("ReadinessProbeFailure")
        if liveness_msgs:
            historical_warnings.append("LivenessProbeFailure")
        if startup_msgs:
            historical_warnings.append("StartupProbeFailure")

    healthy = not issues and phase == "running" and not not_ready
    return {
        "healthy": healthy,
        "issues": issues,
        "historical_warnings": historical_warnings,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _pod_observations(
    *,
    healthy: bool,
    total_restarts: int,
    last_restart_at: str | None = None,
    historical_warnings: list[Any] | None = None,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    if healthy and total_restarts > 0:
        observation: dict[str, Any] = {
            "severity": "info",
            "code": "HISTORICAL_RESTART",
            "message": (
                f"Container restarted {total_restarts} time"
                f"{'s' if total_restarts != 1 else ''} during the pod lifetime"
                + (f"; last restart at {last_restart_at}" if last_restart_at else "")
            ),
            "count": total_restarts,
        }
        if last_restart_at:
            observation["last_restart_at"] = last_restart_at
        observations.append(observation)

    for warning in historical_warnings or []:
        warning_dict = warning if isinstance(warning, dict) else {}
        code = warning_dict.get("type") if warning_dict else str(warning)
        if not code:
            continue
        if warning_dict and code == "PreviousContainerTermination":
            observation = {
                "severity": warning_dict.get("severity") or "info",
                "code": str(code),
                "message": warning_dict.get("message")
                or "Container was previously terminated and the pod recovered",
                "container": warning_dict.get("container"),
                "exit_code": warning_dict.get("exit_code"),
                "reason": warning_dict.get("reason"),
                "finished_at": warning_dict.get("finished_at"),
            }
            observations.append({k: v for k, v in observation.items() if v is not None})
        else:
            observations.append(
                {
                    "severity": "info",
                    "code": str(code),
                    "message": "Historical pod warning observed during startup or rollout",
                }
            )

    return observations


def _pod_recommendations(
    diagnosis_recommendations: list[str],
    *,
    healthy: bool,
    total_restarts: int,
) -> list[str]:
    if healthy and total_restarts > 0 and not diagnosis_recommendations:
        return [
            (
                "No immediate action required. Check the previous container termination reason "
                "if the restart was recent or recurring."
            )
        ]
    return diagnosis_recommendations


def _pod_next_actions(
    *,
    namespace: str,
    pod: str,
    healthy: bool,
    total_restarts: int,
    source_tool: str,
) -> list[dict[str, Any]]:
    if healthy and total_restarts > 0:
        action = "k8s_describe_pod" if source_tool != "k8s_describe_pod" else "k8s_get_pod_logs"
        reason = (
            "Determine the historical restart cause"
            if action == "k8s_describe_pod"
            else "Inspect recent logs only if the restart was recent or recurring"
        )
        next_action: dict[str, Any] = {
            "action": action,
            "priority": "low",
            "reason": reason,
            "tool_arguments": {
                "namespace": namespace,
                "pod": pod,
            },
        }
        if action == "k8s_get_pod_logs":
            next_action["tool_arguments"]["tail_lines"] = 100
        return [next_action]
    return []


def _containers_without_explicit_resources(
    *,
    containers: list[dict[str, Any]],
    resources: dict[str, Any],
) -> list[str]:
    resource_items = resources.get("containers") if isinstance(resources, dict) else None
    if not isinstance(resource_items, list):
        return []
    known_container_names = {str(c.get("name") or "") for c in containers if isinstance(c, dict)}
    missing: list[str] = []
    for item in resource_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in known_container_names:
            continue
        requests = item.get("requests") if isinstance(item.get("requests"), dict) else {}
        limits = item.get("limits") if isinstance(item.get("limits"), dict) else {}
        if not requests and not limits:
            missing.append(name)
    return missing


def _describe_pod_structured(
    pod_raw: dict[str, Any],
    pod_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a structured describe response from raw pod data and filtered events."""
    pod_name = str(pod_raw.get("name") or "")
    namespace = str(pod_raw.get("namespace") or "")
    phase = str(pod_raw.get("phase") or "")

    containers_raw = [c for c in (pod_raw.get("containers") or []) if isinstance(c, dict)]
    containers_out: list[dict[str, Any]] = []
    for c in containers_raw:
        entry: dict[str, Any] = {
            "name": str(c.get("name") or ""),
            "image": _strip_image_digest(str(c.get("image") or "")),
            "ready": bool(c.get("ready")),
            "restart_count": _container_restart_count(c),
        }
        last_restart_at = _format_k8s_timestamp(_container_last_restart_at(c))
        if last_restart_at:
            entry["last_restart_at"] = last_restart_at
        for extra in ("state", "last_state", "started_at"):
            if extra in c:
                entry[extra] = c[extra]
        containers_out.append(entry)

    total_restarts = sum(c["restart_count"] for c in containers_out)
    restart_summary = _restart_window_summary(containers_out)
    all_ready = bool(containers_out) and all(c["ready"] for c in containers_out)

    diagnosis = _diagnose_pod(pod_raw, pod_events)
    observations = _pod_observations(
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        last_restart_at=restart_summary["last_restart_at"],
        historical_warnings=diagnosis.get("historical_warnings"),
    )
    recommendations = _pod_recommendations(
        diagnosis["recommendations"],
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
    )
    next_actions = _pod_next_actions(
        namespace=namespace,
        pod=pod_name,
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        source_tool="k8s_get_pod",
    )
    sorted_events = _sort_events_for_display(_deduplicate_events(pod_events))[:20]

    workload = _workload_from_pod_name(pod_name)
    if diagnosis["healthy"]:
        summary = f"Pod {pod_name} is {phase}, all containers ready, {total_restarts} restarts"
        finding_lines: list[str] = ["✓ Pod is healthy"]
    else:
        issue_types = [i["type"] for i in diagnosis["issues"]]
        summary = f"Pod {pod_name} is {phase} — issues: {', '.join(issue_types)}"
        finding_lines = [
            f"⚠ {i['type']}" + (f" (x{i['count']})" if "count" in i else "")
            for i in diagnosis["issues"]
        ]

    return {
        "status": "success",
        "summary": summary,
        "findings": finding_lines,
        "observations": observations,
        "recommendations": recommendations,
        "next_actions": next_actions,
        "data": {
            "pod": {
                "name": pod_name,
                "namespace": namespace,
                "workload": workload,
                "node": str(pod_raw.get("node_name") or ""),
                "age": str(pod_raw.get("age") or ""),
            },
            "status": {
                "phase": phase,
                "ready": all_ready,
                "restart_count": total_restarts,
                **restart_summary,
            },
            "containers": containers_out,
            "events": [
                {
                    "type": e.get("type"),
                    "reason": e.get("reason"),
                    "message": str(e.get("message") or "")[:200],
                    "count": e.get("count", 1),
                    "last_seen": e.get("last_seen") or e.get("lastSeen"),
                }
                for e in sorted_events
            ],
            "diagnosis": diagnosis,
            "observations": observations,
            "next_actions": next_actions,
        },
    }


def _diagnose_pod_from_description(
    status: dict[str, Any],
    containers: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diagnose from rich k8s.describe_pod data.

    Separates current_issues (pod needs attention now) from historical_warnings
    (problems that occurred during startup/rollout but the pod is now healthy).
    A pod that is Running+Ready with 0 restarts is treated as healthy even if
    probe-failure events exist in its history.
    """
    phase = str(status.get("phase") or "").lower()
    ready = bool(status.get("ready"))
    current_issues: list[dict[str, Any]] = []
    historical_warnings: list[dict[str, Any]] = []
    recommendations: list[str] = []

    total_restarts = sum(
        int(c.get("restart_count") or 0) for c in containers if isinstance(c, dict)
    )

    for c in containers:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        restart_count = int(c.get("restart_count") or 0)

        # Current waiting state — always a live issue
        waiting = (c.get("state") or {}).get("waiting") or {}
        w_reason = str(waiting.get("reason") or "").lower()
        w_message = str(waiting.get("message") or "").lower()
        if "crashloopbackoff" in w_reason:
            current_issues.append(
                {
                    "type": "CrashLoopBackOff",
                    "container": name,
                    "severity": "critical",
                }
            )
            recommendations.append(
                f"Container {name} is crash-looping — run k8s_get_pod_logs to find the crash reason"
            )
        elif "imagepullbackoff" in w_reason or "errimagepull" in w_reason:
            current_issues.append(
                {
                    "type": "ImagePullBackOff",
                    "container": name,
                    "severity": "critical",
                }
            )
            recommendations.append(
                f"Container {name} cannot pull image"
                " — check image name, tag, and registry credentials"
            )
        elif w_reason and "containercreat" not in w_reason:
            current_issues.append(
                {
                    "type": f"ContainerWaiting:{w_reason}",
                    "container": name,
                    "severity": "warning",
                }
            )
            if w_message:
                recommendations.append(f"Container {name} waiting: {w_message[:120]}")

        # Last termination reason (OOMKilled)
        # Historical if pod is now Ready with 0 restarts; current if restarts are ongoing
        last_term = _container_last_termination(c) or {}
        termination_reason = str(last_term.get("reason") or "")
        termination_reason_lower = termination_reason.lower()
        if termination_reason_lower == "oomkilled":
            if restart_count > 0 or not ready or phase != "running":
                current_issues.append(
                    {
                        "type": "OOMKilled",
                        "container": name,
                        "severity": "critical",
                    }
                )
                recommendations.append(
                    f"Container {name} was OOMKilled"
                    " — increase resources.limits.memory or fix memory leak"
                )
            else:
                historical_warnings.append(
                    {
                        "type": "OOMKilled",
                        "container": name,
                        "note": "Pod recovered and is now Ready",
                    }
                )
        elif last_term and restart_count > 0 and phase == "running" and ready:
            exit_code = last_term.get("exit_code") or last_term.get("exitCode")
            finished_at = last_term.get("finished_at") or last_term.get("finishedAt")
            message = (
                f"Container was previously terminated with exit code {exit_code}. "
                "The pod has remained healthy since restart."
            )
            historical_warnings.append(
                {
                    "severity": "info",
                    "type": "PreviousContainerTermination",
                    "container": name,
                    "exit_code": exit_code,
                    "reason": termination_reason,
                    "finished_at": finished_at,
                    "message": message,
                }
            )

    # Pod is currently stable if Running+Ready with no container waiting states
    pod_currently_ok = (
        phase == "running"
        and ready
        and total_restarts == 0
        and not any(i["type"] in {"CrashLoopBackOff", "ImagePullBackOff"} for i in current_issues)
    )

    # Event-based issues — collect per-reason metadata from events
    event_reasons: set[str] = set()
    event_messages: list[str] = []
    probe_events: dict[str, dict[str, Any]] = {}  # probe_type → last event info
    for e in events:
        if not isinstance(e, dict):
            continue
        r = str(e.get("reason") or "").lower()
        if r:
            event_reasons.add(r)
        msg = str(e.get("message") or "").lower()
        event_messages.append(msg)
        if r == "unhealthy":
            for probe in ("readiness", "liveness", "startup"):
                if probe in msg:
                    # Keep the most recent event info for this probe type
                    existing = probe_events.get(probe)
                    if not existing or str(e.get("last_seen") or "") > str(
                        existing.get("last_seen") or ""
                    ):
                        probe_events[probe] = {
                            "last_seen": e.get("last_seen"),
                            "count": int(e.get("count") or 1),
                            "message": str(e.get("message") or "")[:120],
                        }

    if "failedscheduling" in event_reasons:
        if phase == "pending":
            current_issues.append({"type": "FailedScheduling", "severity": "warning"})
            recommendations.append("Check node resources and pod resource requests/taints")
        else:
            historical_warnings.append(
                {
                    "type": "FailedScheduling",
                    "note": "Pod eventually scheduled and is now Running",
                }
            )

    if probe_events:
        probe_type_map = {
            "readiness": "ReadinessProbeFailure",
            "liveness": "LivenessProbeFailure",
            "startup": "StartupProbeFailure",
        }
        for probe, info in probe_events.items():
            issue_type = probe_type_map.get(probe, f"{probe.title()}ProbeFailure")
            if pod_currently_ok:
                # Pod is now healthy — probe failures are rollout/startup noise
                historical_warnings.append(
                    {
                        "type": issue_type,
                        "reason": f"{probe.title()} probe failed during startup/rollout",
                        "last_seen": info.get("last_seen"),
                        "count": info.get("count"),
                    }
                )
            else:
                current_issues.append({"type": issue_type, "severity": "warning"})
                if probe == "readiness":
                    recommendations.append(
                        "Check readiness probe endpoint and application startup time"
                    )
                elif probe == "liveness":
                    recommendations.append("Check liveness probe — container may be restarting")
                elif probe == "startup":
                    recommendations.append(
                        "Startup probe failed — consider increasing initialDelaySeconds"
                    )

    # Phase-level issues
    if phase == "pending" and not any(i["type"] == "FailedScheduling" for i in current_issues):
        if not any("imagepull" in str(i["type"]).lower() for i in current_issues):
            current_issues.append({"type": "Pending", "severity": "warning"})
            recommendations.append(
                "Pod is waiting — check events for scheduling or image pull issues"
            )
    elif phase == "failed":
        current_issues.append({"type": "PodFailed", "severity": "critical"})
        recommendations.append("Pod is in Failed state — check logs for exit reason")
    elif phase == "unknown":
        current_issues.append({"type": "UnknownPhase", "severity": "warning"})
        recommendations.append("Node may be unreachable — check node status")

    # Containers not ready with no specific cause yet detected
    not_ready = [c.get("name") for c in containers if isinstance(c, dict) and not c.get("ready")]
    if not_ready and not current_issues and phase == "running":
        current_issues.append(
            {
                "type": "ContainersNotReady",
                "containers": not_ready,
                "severity": "warning",
            }
        )
        recommendations.append(
            "Containers are not ready — check readiness probe and application startup"
        )

    healthy = not current_issues and phase == "running" and not not_ready
    return {
        "healthy": healthy,
        "current_issues": current_issues,
        "historical_warnings": historical_warnings,
        # keep "issues" as alias so existing callers don't break
        "issues": current_issues,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _build_describe_response(
    desc: dict[str, Any],
    *,
    include_details: bool = False,
) -> dict[str, Any]:
    """Build the MCP k8s_describe_pod response from a k8s.describe_pod agent payload."""
    meta = desc.get("metadata") or {}
    status = desc.get("status") or {}
    containers = [c for c in (desc.get("containers") or []) if isinstance(c, dict)]
    resources = desc.get("resources") or {}
    probes = desc.get("probes") or []
    events = [e for e in (desc.get("events") or []) if isinstance(e, dict)]

    pod_name = str(meta.get("name") or "")
    phase = str(status.get("phase") or "")
    total_restarts = sum(int(c.get("restart_count") or 0) for c in containers)
    restart_summary = _restart_window_summary(containers)

    diagnosis = _diagnose_pod_from_description(status, containers, events)
    workload = _workload_from_pod_name(pod_name)

    historical = diagnosis.get("historical_warnings") or []
    observations = _pod_observations(
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        last_restart_at=restart_summary["last_restart_at"],
        historical_warnings=historical,
    )
    recommendations = _pod_recommendations(
        diagnosis["recommendations"],
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
    )
    next_actions = _pod_next_actions(
        namespace=str(meta.get("namespace") or ""),
        pod=pod_name,
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        source_tool="k8s_describe_pod",
    )

    if diagnosis["healthy"]:
        if historical:
            hw_types = list(dict.fromkeys(w["type"] for w in historical))
            summary = (
                f"Pod {pod_name} is currently healthy"
                f"; historical warnings found during startup/rollout: {', '.join(hw_types)}"
            )
            findings: list[str] = [
                "✓ Pod is Running and Ready",
                f"✓ {total_restarts} restarts",
            ] + [
                "~ Historical: "
                + w["type"]
                + (f" ({w.get('reason', '')})" if w.get("reason") else "")
                for w in historical
            ]
        else:
            summary = f"Pod {pod_name} is {phase}, all containers ready, {total_restarts} restarts"
            findings = ["✓ Pod is healthy"]
    else:
        issue_types = [i["type"] for i in diagnosis["current_issues"]]
        summary = f"Pod {pod_name} is {phase} — issues: {', '.join(issue_types)}"
        findings = [
            f"⚠ {i['type']}" + (f" (container: {i['container']})" if "container" in i else "")
            for i in diagnosis["current_issues"]
        ]

    containers_missing_resources = _containers_without_explicit_resources(
        containers=containers,
        resources=resources if isinstance(resources, dict) else {},
    )
    for container_name in containers_missing_resources:
        findings.append(f"⚠ {container_name} has no explicit CPU or memory requests/limits")

    container_summaries = []
    for c in containers:
        container_summary = {
            "name": str(c.get("name") or ""),
            "ready": bool(c.get("ready")),
            "restart_count": int(c.get("restart_count") or 0),
            "image": _strip_image_digest(str(c.get("image") or "")),
        }
        last_restart_at = _format_k8s_timestamp(_container_last_restart_at(c))
        if last_restart_at:
            container_summary["last_restart_at"] = last_restart_at
        container_summaries.append(container_summary)
    pod_summary = {
        "name": pod_name,
        "namespace": str(meta.get("namespace") or ""),
        "workload": workload,
        "owner": str(meta.get("owner") or ""),
        "age": str(meta.get("age") or ""),
    }
    if include_details:
        pod_summary["node"] = str(meta.get("node") or "")
        pod_summary["pod_ip"] = str(meta.get("pod_ip") or "")

    data: dict[str, Any] = {
        "pod": pod_summary,
        "status": {
            "phase": phase,
            "ready": bool(status.get("ready")),
            "conditions": status.get("conditions") or [],
            "restart_count": total_restarts,
            **restart_summary,
            "reason": str(status.get("reason") or ""),
            "message": str(status.get("message") or ""),
        },
        "containers": container_summaries,
        "events": [
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "message": str(e.get("message") or "")[:200],
                "count": e.get("count", 1),
                "last_seen": e.get("last_seen"),
            }
            for e in events[:20]
        ],
        "diagnosis": diagnosis,
        "observations": observations,
        "next_actions": next_actions,
    }
    if include_details:
        data["resources"] = resources
        data["probes"] = probes

    return {
        "status": "success",
        "summary": summary,
        "findings": findings,
        "observations": observations,
        "recommendations": recommendations,
        "next_actions": next_actions,
        "data": data,
    }


def _unhealthy_pod_entry(pod: dict[str, Any]) -> dict[str, Any]:
    """Build a rich unhealthy pod summary with likely cause and next action."""
    phase = str(pod.get("phase") or "")
    containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
    not_ready = [c for c in containers if not c.get("ready")]
    total_restarts = sum(_container_restart_count(c) for c in containers)

    if phase.lower() == "pending":
        reason = "Pending"
        likely_cause = "Pod is waiting to be scheduled or pulling an image"
        recommendation = (
            "Run k8s_describe_pod then check events for FailedScheduling or ImagePullBackOff"
        )
    elif phase.lower() == "failed":
        reason = "Failed"
        likely_cause = "Container exited with a non-zero exit code"
        recommendation = "Run k8s_debug_pod to find the crash reason in logs"
    elif phase.lower() == "unknown":
        reason = "Unknown"
        likely_cause = "Node is unreachable or the agent cannot contact the Kubernetes API"
        recommendation = "Check node status and cluster connectivity"
    elif total_restarts > 5:
        reason = f"CrashLoopBackOff (restarts: {total_restarts})"
        likely_cause = "Container is crashing repeatedly"
        recommendation = "Run k8s_debug_pod to investigate logs and crash cause"
    elif not_ready:
        not_ready_names = [str(c.get("name") or "") for c in not_ready]
        reason = f"Containers not ready: {', '.join(not_ready_names)}"
        likely_cause = "Readiness probe is failing or application is still starting up"
        recommendation = "Run k8s_describe_pod to see events and k8s_get_pod_logs for errors"
    else:
        reason = f"Phase: {phase}"
        likely_cause = "Unexpected pod state"
        recommendation = (
            "No immediate action required if the pod is Running and Ready. "
            "Check the previous container termination reason if restarts are recent or recurring."
        )

    return {
        "name": str(pod.get("name") or ""),
        "namespace": str(pod.get("namespace") or ""),
        "phase": phase,
        "reason": reason,
        "restart_count": total_restarts,
        "age": str(pod.get("age") or ""),
        "likely_cause": likely_cause,
        "recommendation": recommendation,
    }


def _cluster_health_assessment(overview: dict[str, Any]) -> dict[str, Any]:
    """Derive cluster health, findings, and recommendations from an overview payload."""
    findings: list[str] = []
    recommendations: list[str] = []

    unhealthy_count = int(overview.get("pods_unhealthy") or 0)
    total_pods = int(overview.get("pods_total") or 0)
    ws = overview.get("warning_event_summary") or {}
    active_warnings = int(ws.get("active_warning_events") or 0)
    top_restarts = overview.get("top_restarts") or []

    if unhealthy_count == 0:
        findings.append("✓ No unhealthy pods")
    else:
        findings.append(f"⚠ {unhealthy_count} unhealthy pod{'s' if unhealthy_count != 1 else ''}")
        for pod in (overview.get("unhealthy_pods") or [])[:3]:
            findings.append(f"  - {pod.get('pod')}: {pod.get('phase')}")
        recommendations.append(
            f"Investigate {unhealthy_count} unhealthy pod(s) with k8s_show_unhealthy_pods"
        )

    if active_warnings == 0:
        findings.append("✓ No active warning events")
    else:
        findings.append(
            f"⚠ {active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
        )
        recommendations.append("Review active warning events with k8s_list_events")

    high_restart = [p for p in top_restarts if int(p.get("restarts") or 0) > 5]
    if high_restart:
        for p in high_restart[:3]:
            findings.append(f"⚠ {p.get('pod')} has {p.get('restarts')} restarts")
        recommendations.append("Investigate high-restart pods with k8s_debug_pod")
    elif not high_restart and unhealthy_count == 0:
        findings.append("✓ No high-restart pods")

    # Three-tier classification:
    # Degraded  — unhealthy pods, high-restart pods, or crashed workloads
    # Warning   — all pods healthy but warning events are present
    # Healthy   — all pods healthy, no warnings
    if unhealthy_count > 0 or high_restart:
        cluster_health = "Degraded"
        summary = (
            f"{unhealthy_count}/{total_pods} pods unhealthy, "
            f"{active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
        )
    elif active_warnings > 0:
        cluster_health = "Warning"
        summary = (
            f"All {total_pods} pod{'s' if total_pods != 1 else ''} healthy"
            f", but {active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
            " present. Review events to confirm they are not current failures."
        )
        recommendations.append("Review active warning events with k8s_list_events")
    else:
        cluster_health = "Healthy"
        summary = f"All {total_pods} pods running normally"

    return {
        "cluster_health": cluster_health,
        "summary": summary,
        "findings": findings,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


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


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(r"(redis://)([^:@\s]+:)?([^@\s]+)@", r"\1***@", value)
    redacted = re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)=([^\s,;]+)",
        r"\1=***",
        redacted,
    )
    return redacted


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
    noisy_patterns = (
        "debug",
        "httpcore.",
        "httpx",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
        "sse_starlette.sse",
        "raw response",
    )
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
        redacted_line = _redact_sensitive_text(line)
        lowered = redacted_line.lower()
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
        selected.append(redacted_line)

    highlighted = [
        line for line in selected if any(pattern in line.lower() for pattern in important_patterns)
    ]
    compact_data = dict(payload.get("data") if isinstance(payload.get("data"), dict) else {})
    compact_data.pop("logs", None)
    compact_data.pop("log", None)
    compact_data.pop("text", None)
    compact_data.pop("output", None)
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
    truncated = len(selected) > 120
    if truncated:
        compact_data["truncated"] = True
    return {
        **payload,
        "truncated": bool(payload.get("truncated")) or truncated,
        "data": compact_data,
    }


_INTERNAL_LOGGER_PATTERNS = (
    "httpcore.",
    "httpx",
    "platform_api.domain.services.agent_registry_service",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http",
    "sse_starlette.sse",
)


def _redact_platform_internal_log_line(line: str) -> str:
    redacted = _redact_sensitive_text(line)
    redacted = re.sub(r"\b[\w.-]+\.svc\.cluster\.local\b", "<internal-service>", redacted)
    redacted = re.sub(
        r"\b(workspace_id|agent_id|cluster_id|request_id|command_id)=['\"]?[\w:.-]+",
        r"\1=<redacted>",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r'"(workspace_id|agent_id|cluster_id|request_id|command_id)"\s*:\s*"[^"]+"',
        r'"\1":"<redacted>"',
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"/internal/[A-Za-z0-9_./-]+", "/internal/<redacted>", redacted)
    return redacted


def _log_category(line: str, *, exclude_loggers: list[str] | None = None) -> str:
    lowered = line.lower()
    extra_patterns = tuple(pattern.lower().rstrip("*") for pattern in (exclude_loggers or []))
    if any(pattern in lowered for pattern in (*_INTERNAL_LOGGER_PATTERNS, *extra_patterns)):
        return "internal_debug"
    if any(token in lowered for token in ("status_code", "method=", "path=", "http request")):
        return "http_access"
    dependency_tokens = ("redis", "postgres", "database", "upstream", "dependency")
    if any(token in lowered for token in dependency_tokens):
        return "dependency"
    return "application"


def _log_pattern(line: str) -> str:
    text = _redact_platform_internal_log_line(line)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        event = parsed.get("event") or parsed.get("message") or parsed.get("msg")
        if event:
            return str(event)[:120]
    match = re.search(r"\bevent=['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1)[:120]
    simplified = re.sub(r"\b\d+(?:\.\d+)?\b", "<num>", text)
    simplified = re.sub(r"\s+", " ", simplified).strip()
    return simplified[:120] or "unclassified log line"


def _extract_latency_ms(line: str) -> float | None:
    patterns = (
        r"\b(?:duration|duration_ms|latency|latency_ms|elapsed_ms)=['\"]?([0-9]+(?:\.[0-9]+)?)",
        r'"(?:duration_ms|latency_ms|elapsed_ms)"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r"\bin\s+([0-9]+(?:\.[0-9]+)?)\s*ms\b",
    )
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _analyze_workload_logs(
    logs_data: dict[str, Any] | None,
    *,
    exclude_loggers: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(logs_data, dict):
        return {
            "lines_scanned": 0,
            "errors": 0,
            "warnings": 0,
            "top_patterns": [],
            "latency": {"p50_ms": None, "max_ms": None},
            "notable_lines": [],
            "log_categories": {
                "application": 0,
                "http_access": 0,
                "dependency": 0,
                "internal_debug": 0,
            },
        }

    raw_lines = [str(line) for line in (logs_data.get("lines") or []) if isinstance(line, str)]
    category_counts = {
        "application": 0,
        "http_access": 0,
        "dependency": 0,
        "internal_debug": int(logs_data.get("skipped_debug_lines") or 0),
    }
    pattern_counts: dict[str, int] = {}
    notable_lines: list[str] = []
    latency_values: list[float] = []
    error_count = 0
    warning_count = 0

    for line in raw_lines:
        redacted_line = _redact_platform_internal_log_line(line)
        lowered = redacted_line.lower()
        category = _log_category(redacted_line, exclude_loggers=exclude_loggers)
        category_counts[category] = category_counts.get(category, 0) + 1

        if any(token in lowered for token in ("error", "exception", "traceback", "fatal", "panic")):
            error_count += 1
            notable_lines.append(redacted_line)
        elif any(token in lowered for token in ("warn", "warning", "failed", "timeout")):
            warning_count += 1
            notable_lines.append(redacted_line)

        latency_ms = _extract_latency_ms(redacted_line)
        if latency_ms is not None:
            latency_values.append(latency_ms)

        if category != "internal_debug":
            pattern = _log_pattern(redacted_line)
            pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    latency_values.sort()
    p50_ms: float | None = None
    if latency_values:
        p50_ms = latency_values[len(latency_values) // 2]

    top_patterns = [
        {"event": event, "count": count}
        for event, count in sorted(pattern_counts.items(), key=lambda item: item[1], reverse=True)[
            :5
        ]
    ]
    return {
        "lines_scanned": int(logs_data.get("line_count") or len(raw_lines)),
        "errors": error_count,
        "warnings": warning_count,
        "top_patterns": top_patterns,
        "latency": {
            "p50_ms": p50_ms,
            "max_ms": max(latency_values) if latency_values else None,
        },
        "notable_lines": notable_lines[-10:],
        "log_categories": category_counts,
    }


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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "async",
        "job_id": job_id,
        "status": status,
        "poll_after_seconds": poll_after_seconds,
    }
    if extra:
        payload.update(extra)
    return payload


def _compact_incident(incident: Any) -> dict[str, Any]:
    if not isinstance(incident, dict):
        return {"name": str(incident)}

    latest_update = None
    updates = incident.get("incident_updates")
    if isinstance(updates, list) and updates:
        update_dicts = [item for item in updates if isinstance(item, dict)]
        latest_update = max(
            update_dicts,
            key=lambda item: (
                _compact_incident_update_timestamp(item) or datetime.min.replace(tzinfo=UTC)
            ),
            default=None,
        )

    return {
        "id": incident.get("id"),
        "name": incident.get("name") or incident.get("title"),
        "status": incident.get("status"),
        "impact": incident.get("impact"),
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "shortlink": incident.get("shortlink") or incident.get("link"),
        "updates_count": incident.get("updates_count")
        or (len(updates) if isinstance(updates, list) else None),
        "latest_update_status": latest_update.get("status") if latest_update else None,
        "latest_update_at": (
            latest_update.get("updated_at")
            or latest_update.get("created_at")
            or latest_update.get("display_at")
            if latest_update
            else None
        ),
    }


def _compact_incident_update_timestamp(update: dict[str, Any]) -> datetime | None:
    timestamp = update.get("updated_at") or update.get("created_at") or update.get("display_at")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    active_incidents = [incident for incident in compact_incidents if _incident_is_active(incident)]
    all_historical_incidents = [
        incident for incident in compact_incidents if not _incident_is_active(incident)
    ]
    max_historical_incidents = 5
    historical_incidents = all_historical_incidents[:max_historical_incidents]
    historical_total = max(0, len(incidents_list) - len(active_incidents))

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
        "historical_incidents_total": historical_total,
        "degraded_components": compact_degraded,
        "fetched_at": provider_status.get("fetched_at"),
        "truncated": len(incidents_list) > len(active_incidents) + len(historical_incidents),
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
                "source_url": error.get("source_url"),
                "status_code": error.get("status_code"),
                "active_incidents": [],
                "historical_incidents": [],
                "historical_incidents_total": 0,
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
) -> dict[str, Any]:
    status = str(job.get("status", "unknown"))
    if not _polled_job_matches(job, expected_job_type="alert.group.summary.generate"):
        return _polled_job_mismatch_result(
            job_id=job_id,
            status=status,
            expected_tool="external_status_check",
            expected_job_type="alert.group.summary.generate",
        )

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
            return (
                normalized_result
                if isinstance(normalized_result, dict)
                else {"status": status, "result": normalized_result}
            )
        payload: dict[str, Any] = {
            "mode": "completed",
            "job_id": job_id,
            "status": status,
            "result": normalized_result,
            "error": job.get("error"),
            "artifact_refs": _safe_artifact_refs(job.get("artifact_refs", [])),
            "usage": job.get("usage"),
            "updated_at": job.get("updated_at"),
            "response_mode": response_mode,
        }
        return payload

    return _build_async_result(
        job_id=job_id,
        status=status,
        poll_after_seconds=poll_after_seconds,
    )


def _normalize_polled_incident_summary_job(
    *,
    job_id: str,
    job: dict[str, Any],
    poll_after_seconds: int,
) -> dict[str, Any]:
    status = str(job.get("status", "unknown"))
    if not _polled_job_matches(job, expected_job_type="incident.summary.generate"):
        return _polled_job_mismatch_result(
            job_id=job_id,
            status=status,
            expected_tool="incident_summary",
            expected_job_type="incident.summary.generate",
        )

    if status in _TERMINAL_JOB_STATUSES:
        payload: dict[str, Any] = {
            "mode": "completed",
            "job_id": job_id,
            "status": status,
            "result": job.get("result"),
            "error": job.get("error"),
            "artifact_refs": _safe_artifact_refs(job.get("artifact_refs", [])),
            "usage": job.get("usage"),
            "updated_at": job.get("updated_at"),
        }
        return payload

    # Still in flight (admitted/queued/dispatched/running or unknown) — report status.
    return _build_async_result(
        job_id=job_id,
        status=status,
        poll_after_seconds=poll_after_seconds,
    )


def _safe_artifact_refs(artifact_refs: Any) -> list[str]:
    if not isinstance(artifact_refs, list):
        return []
    return [
        artifact_ref
        for artifact_ref in artifact_refs
        if isinstance(artifact_ref, str) and not artifact_ref.startswith("mock_")
    ]


def _polled_job_matches(job: dict[str, Any], *, expected_job_type: str) -> bool:
    observed_type = job.get("job_type") or job.get("type") or job.get("operation")
    if isinstance(observed_type, str):
        return observed_type == expected_job_type

    result = job.get("result")
    if not isinstance(result, dict):
        return True

    if expected_job_type == "incident.summary.generate":
        return "external_status" not in result and result.get("action") != "fetched_external_status"
    if expected_job_type == "alert.group.summary.generate":
        return "external_status" in result or result.get("action") == "fetched_external_status"
    return True


def _polled_job_mismatch_result(
    *,
    job_id: str,
    status: str,
    expected_tool: str,
    expected_job_type: str,
) -> dict[str, Any]:
    return {
        "mode": "completed",
        "job_id": job_id,
        "status": "failed",
        "error": {
            "code": "JOB_OPERATION_MISMATCH",
            "message": (
                "check_id belongs to a different async operation; start a new "
                f"{expected_tool} job or poll the matching tool."
            ),
            "expected_job_type": expected_job_type,
            "observed_status": status,
        },
    }


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
) -> dict[str, Any]:
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

    async def _resolve_tool_guard(
        tool_name: str,
    ) -> ResolvedIntegrationContext | str | None:
        return await resolve_tool_integration_context(
            tool=_specs[tool_name],
            principal=_current_principal(settings),
            settings=settings,
        )

    async def _require_k8s_context(tool_name: str) -> ResolvedIntegrationContext | str | None:
        return await _resolve_tool_guard(tool_name)

    @mcp.tool(**_tool_metadata(_specs["incidentflow_capabilities"]))
    async def incidentflow_capabilities(
        response_mode: str = "compact",
        category: str | None = None,
    ) -> dict[str, Any]:
        return _incidentflow_capabilities_payload(response_mode=response_mode, category=category)

    @mcp.tool(**_tool_metadata(_specs["mcp_version"]))
    async def mcp_version() -> dict[str, Any]:
        return _mcp_version_payload(settings)

    @mcp.tool(**_tool_metadata(_specs["incidentflow_auth_status"]))
    async def incidentflow_auth_status() -> dict[str, Any]:
        return await _incidentflow_auth_status_payload(
            settings=settings,
            principal=_current_principal(settings),
        )

    @mcp.tool(**_tool_metadata(_specs["incidentflow_integrations_status"]))
    async def incidentflow_integrations_status() -> dict[str, Any]:
        return await _incidentflow_integrations_status_payload(
            settings=settings,
            principal=_current_principal(settings),
        )

    from incidentflow_mcp.tools.knowledge_search_tools import (
        KnowledgeSearchAPIError,
        knowledge_get,
        private_knowledge_search,
        public_knowledge_search,
    )

    @mcp.tool(**_tool_metadata(_specs["public_knowledge_search"]))
    async def public_knowledge_search_tool(
        query: str,
        document_type: str | None = None,
        response_mode: str = "compact",
        limit: int = 8,
    ) -> dict[str, Any]:
        try:
            return await public_knowledge_search(
                settings=settings,
                query=query,
                document_type=document_type,
                response_mode=response_mode,
                limit=limit,
            )
        except KnowledgeSearchAPIError as exc:
            return {"error": str(exc)}

    @mcp.tool(**_tool_metadata(_specs["private_knowledge_search"]))
    async def private_knowledge_search_tool(
        query: str,
        document_type: str | None = None,
        service: str | None = None,
        environment: str | None = None,
        response_mode: str = "compact",
        limit: int = 8,
    ) -> dict[str, Any]:
        try:
            return await private_knowledge_search(
                settings=settings,
                workspace_id=_workspace(),
                query=query,
                document_type=document_type,
                service=service,
                environment=environment,
                response_mode=response_mode,
                limit=limit,
            )
        except (KnowledgeSearchAPIError, ValueError) as exc:
            return {"error": str(exc)}

    @mcp.tool(**_tool_metadata(_specs["knowledge_get"]))
    async def knowledge_get_tool(
        id: str,
        id_type: str = "auto",
        document_type: str | None = None,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        try:
            return await knowledge_get(
                settings=settings,
                workspace_id=_workspace(),
                id=id,
                id_type=id_type,
                document_type=document_type,
                response_mode=response_mode,
            )
        except (KnowledgeSearchAPIError, ValueError) as exc:
            return {"error": str(exc)}

    @mcp.tool(**_tool_metadata(_specs["incident_summary"]))
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
            if wait_for_result and str(job.get("status", "")) not in _TERMINAL_JOB_STATUSES:
                job = await _poll_until_done(
                    client=client,
                    job_id=check_id,
                    initial_delay=settings.platform_api_ai_poll_after_seconds,
                    max_wait_seconds=45,
                )
            return _normalize_polled_incident_summary_job(
                job_id=check_id,
                job=job,
                poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            )

        if not incident_id.strip():
            raise ValueError("incident_id is required unless check_id is provided")

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
            data = result.model_dump(mode="json")
            query = f"{result.title} {result.summary}".strip()
            service = result.affected_services[0] if result.affected_services else None
            ctx = await _consult_memory(query=query, service=service, workspace_id=workspace_id)
            if ctx:
                data["memory_context"] = ctx
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
        return _build_async_result(
            job_id=submitted["job_id"],
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        )

    @mcp.tool(**_tool_metadata(_specs["correlate_alerts"]))
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
        normalized_alerts = _normalize_correlation_alerts(alerts, alerts_json)
        input_data = CorrelateAlertsInput(
            alerts=normalized_alerts,
            window_minutes=window_minutes,
            min_cluster_size=min_cluster_size,
        )
        _resolve_correlation_mode(execution_mode)
        result: CorrelateAlertsOutput = _correlate_alerts_impl(input_data)

        data = result.model_dump(mode="json")
        # Consult memory using the alert names + dominant service as the signature.
        if normalized_alerts:
            names = [a.name for a in normalized_alerts if a.name]
            services = [a.service for a in normalized_alerts if a.service]
            dominant_service = max(set(services), key=services.count) if services else None
            query = " ".join(dict.fromkeys(names)) or "alert correlation"
            ctx = await _consult_memory(
                query=query, service=dominant_service, workspace_id=workspace_id
            )
            if ctx:
                data["memory_context"] = ctx
        return data

    @mcp.tool(**_tool_metadata(_specs["external_status_check"]))
    async def external_status_check(
        providers: list[str] | None = None,
        execution_mode: str = "async",
        workspace_id: str | None = None,
        check_id: str | None = None,
        wait_for_result: bool = True,
        days_back: int = 30,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
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
        deduplicate: bool = True,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("slack_alerts_list")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _structured_guard_error(_workspace_context_required_error())

        try:
            selected_channel = (channel or settings.slack_alerts_channel).strip() or "alerts"
            selected_limit = settings.slack_alerts_default_limit if limit is None else limit
            if selected_limit < 1 or selected_limit > 200:
                raise ValueError("limit must be between 1 and 200")
            selected_thread_mode = _normalize_slack_thread_mode(thread_mode)
            if max_thread_replies < 0 or max_thread_replies > 200:
                raise ValueError("max_thread_replies must be between 0 and 200")
            token, platform_client = _resolve_slack_tool_access(
                settings,
                workspace_id=workspace_id,
                token_workspace_id=token_workspace_id,
            )

            result = await fetch_slack_alerts(
                token=token,
                channel=selected_channel,
                limit=selected_limit,
                include_raw=include_raw,
                include_threads=include_threads,
                thread_mode=selected_thread_mode,  # type: ignore[arg-type]
                max_thread_replies=max_thread_replies,
                include_system_messages=include_system_messages,
                deduplicate=deduplicate,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return _structured_guard_error(_platform_slack_error_json(exc))
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["slack_alert_thread_get"]))
    async def slack_alert_thread_get(
        channel_id: str,
        message_ts: str,
        include_root: bool = True,
        include_raw: bool = False,
        max_replies: int = 50,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("slack_alert_thread_get")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _structured_guard_error(_workspace_context_required_error())

        try:
            if max_replies < 0 or max_replies > 200:
                raise ValueError("max_replies must be between 0 and 200")
            token, platform_client = _resolve_slack_tool_access(
                settings,
                workspace_id=workspace_id,
                token_workspace_id=token_workspace_id,
            )

            result = await fetch_slack_alert_thread(
                token=token,
                channel_id=channel_id,
                message_ts=message_ts,
                include_root=include_root,
                include_raw=include_raw,
                max_replies=max_replies,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return _structured_guard_error(_platform_slack_error_json(exc))
        return result.model_dump(mode="json")

    async def _auto_upsert_thread_summary(
        *,
        workspace_id: str,
        channel_id: str,
        thread_ts: str,
        result: dict[str, Any],
        alert_context: IncidentThreadAlertContext | None,
    ) -> None:
        """Fire-and-forget: embed slack thread summary into Qdrant memory."""
        try:
            from incidentflow_mcp.tools.memory_tools import PlatformAPIMemoryClient

            # Build rich embedding text from all available summary fields
            parts: list[str] = []
            if title := result.get("title"):
                parts.append(str(title))
            if summary := result.get("summary"):
                parts.append(str(summary))
            if rca := result.get("probable_root_cause"):
                parts.append(f"Root cause: {rca}")
            if actions := result.get("actions_taken"):
                if isinstance(actions, list) and actions:
                    parts.append(f"Actions: {', '.join(str(a) for a in actions[:5])}")

            text = ". ".join(filter(None, parts)).strip()
            if not text:
                return

            # Stable deterministic incident_id from channel + thread_ts
            incident_id = f"slack:{channel_id}:{thread_ts}"

            service = alert_context.service if alert_context else None
            severity = alert_context.severity if alert_context else None
            status = result.get("status")

            mem = PlatformAPIMemoryClient(settings)
            await mem.upsert(
                workspace_id=workspace_id,
                incident_id=incident_id,
                source="slack_thread",
                text=text,
                service=service,
                severity=severity,
                status=status,
            )
            logger.info(
                "memory: auto-upserted slack thread workspace=%s incident=%s service=%s",
                workspace_id,
                incident_id,
                service,
            )
        except Exception:
            # Never block the main response — log and move on
            logger.warning("memory: failed to auto-upsert thread summary", exc_info=True)

    async def _consult_memory(
        *,
        query: str,
        service: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        tags: list[str] | None = None,
        workspace_id: str | None = None,
        score_threshold: float = 0.55,
    ) -> dict[str, Any] | None:
        """Best-effort semantic memory lookup to enrich a diagnostic tool's response.

        Bounded (single search, wall-clock capped) and non-fatal: returns a
        memory_context dict to embed in the response, or None when the feature is
        disabled, memory is unavailable, no workspace resolves, or nothing relevant
        was found. Never raises — a memory failure must not break the diagnostic.
        """
        if not settings.mcp_memory_consult_enabled:
            return None
        if not settings.platform_api_base_url:
            return None
        if not (query and query.strip()):
            return None
        try:
            wid = _workspace(workspace_id)
        except ValueError:
            return None
        try:
            from incidentflow_mcp.tools.memory_tools import memory_consult

            return await asyncio.wait_for(
                memory_consult(
                    settings,
                    wid,
                    query,
                    service=service,
                    cluster=cluster,
                    namespace=namespace,
                    tags=tags,
                    score_threshold=score_threshold,
                ),
                timeout=settings.platform_api_timeout_seconds,
            )
        except Exception:
            logger.warning("memory: consult failed", exc_info=True)
            return None

    async def _consult_pod_memory(
        describe: dict[str, Any], *, pod: str, namespace: str
    ) -> dict[str, Any] | None:
        """Shared consult for pod-describe results (k8s_describe_pod / k8s_debug_pod).

        Builds the query from the detected failure signature and only consults when the
        pod actually looks unhealthy (issues present or not ready).
        """
        data = describe.get("data") or {}
        diagnosis = data.get("diagnosis") or {}
        status = data.get("status") or {}
        issue_types = [
            str(i.get("type"))
            for i in (diagnosis.get("current_issues") or [])
            if isinstance(i, dict) and i.get("type")
        ]
        not_ready = not bool(status.get("ready"))
        if not (issue_types or not_ready):
            return None
        query = " ".join([*issue_types, pod, namespace]).strip() or f"{pod} {namespace}"
        return await _consult_memory(query=query, namespace=namespace)

    @mcp.tool(**_tool_metadata(_specs["incident_thread_summary"]))
    async def incident_thread_summary(
        channel_id: str,
        thread_ts: str,
        alert_context: IncidentThreadAlertContext | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("incident_thread_summary")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        token_workspace_id = _current_token_workspace_id()
        if not token_workspace_id:
            return _structured_guard_error(_workspace_context_required_error())

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
            return _structured_guard_error(_platform_slack_error_json(exc))

        # Auto-persist to semantic memory — non-blocking, never delays the response
        asyncio.create_task(  # noqa: RUF006
            _auto_upsert_thread_summary(
                workspace_id=token_workspace_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                result=result,
                alert_context=alert_context,
            )
        )

        return result

    @mcp.tool(**_tool_metadata(_specs["k8s_connection_health"]))
    async def k8s_connection_health(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_connection_health")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        client = PlatformAPIAgentCommandsClient(settings)
        return _with_integration_context(
            await _k8s_connection_health_payload(
                client=client,
                bearer_token=_current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
                timeout_seconds=timeout_seconds,
            ),
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_cluster_overview"]))
    async def k8s_cluster_overview(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_cluster_overview")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
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

    @mcp.tool(**_tool_metadata(_specs["k8s_namespace_overview"]))
    async def k8s_namespace_overview(
        namespace: str,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_namespace_overview")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
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

    @mcp.tool(**_tool_metadata(_specs["k8s_rbac_check"]))
    async def k8s_rbac_check(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_rbac_check")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
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
        return _with_integration_context(
            payload,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_agent_status"]))
    async def k8s_agent_status(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_agent_status")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        if isinstance(guard, ResolvedIntegrationContext) and guard.source == "shared_dev":
            cluster_id = cluster_id or guard.resource_id
        _ = timeout_seconds
        client = PlatformAPIAgentCommandsClient(settings)
        return _with_integration_context(
            await _k8s_agent_status_payload(
                client=client,
                bearer_token=_current_bearer_token(),
                cluster_id=cluster_id,
                environment=environment,
                cluster_name=cluster_name,
            ),
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_list_namespaces"]))
    async def k8s_list_namespaces(
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_list_namespaces")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
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

    @mcp.tool(**_tool_metadata(_specs["k8s_list_pods"]))
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
        guard = await _require_k8s_context("k8s_list_pods")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
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

    @mcp.tool(**_tool_metadata(_specs["k8s_get_pod"]))
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
        guard = await _require_k8s_context("k8s_get_pod")
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

    @mcp.tool(**_tool_metadata(_specs["k8s_get_pod_logs"]))
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
        guard = await _require_k8s_context("k8s_get_pod_logs")
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

    @mcp.tool(**_tool_metadata(_specs["k8s_list_events"]))
    async def k8s_list_events(
        namespace: str | None = None,
        pod: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_list_events")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
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

    @mcp.tool(**_tool_metadata(_specs["k8s_list_deployments"]))
    async def k8s_list_deployments(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_list_deployments")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
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

    @mcp.tool(**_tool_metadata(_specs["k8s_list_services"]))
    async def k8s_list_services(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_list_services")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        raw = await _send_k8s_agent_command(
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

    @mcp.tool(**_tool_metadata(_specs["k8s_get_rollout_status"]))
    async def k8s_get_rollout_status(
        namespace: str,
        deployment: str | None = None,
        workload: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_get_rollout_status")
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

    @mcp.tool(**_tool_metadata(_specs["k8s_show_unhealthy_pods"]))
    async def k8s_show_unhealthy_pods(
        namespace: str | None = None,
        environment: str | None = None,
        cluster_name: str | None = None,
        cluster_id: str | None = None,
        include_memory_context: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_show_unhealthy_pods")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        result = await _fetch_pods_for_analysis(
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
            ctx = await _consult_memory(query=query, namespace=namespace)
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
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
        tail_lines: Annotated[int, Field(ge=1, le=1000)] = 100,
        include_memory_context: bool = True,
        include_raw_logs: bool = False,
        exclude_loggers: list[str] | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        guard = await _require_k8s_context("k8s_analyze_workload")
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
            ctx = await _consult_memory(
                query=f"{workload} {namespace} rollout pod failure", namespace=namespace
            )
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    def _argocd_client() -> PlatformArgoCDClient:
        resolved_workspace_id = _resolve_job_workspace_id(
            None,
            token_workspace_id=_current_token_workspace_id(),
            default_workspace_id=settings.mcp_default_workspace_id,
        )
        return PlatformArgoCDClient(settings, workspace_id=resolved_workspace_id)

    @mcp.tool(**_tool_metadata(_specs["argocd_connection_health"]))
    async def argocd_connection_health(integration_id: str | None = None) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_connection_health")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_connection_health(
                _argocd_client(), integration_id=integration_id
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_list_applications"]))
    async def argocd_list_applications(
        integration_id: str | None = None,
        search: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        destination_cluster: str | None = None,
        health_status: str | None = None,
        sync_status: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_list_applications")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_list_applications(
                _argocd_client(),
                integration_id=integration_id,
                search=search,
                project=project,
                namespace=namespace,
                destination_cluster=destination_cluster,
                health_status=health_status,
                sync_status=sync_status,
                limit=limit,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_get_application"]))
    async def argocd_get_application(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        history_limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_get_application")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_get_application(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                response_mode=response_mode,
                history_limit=history_limit,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_get_application_resources"]))
    async def argocd_get_application_resources(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        response_mode: Literal["compact", "full"] = "compact",
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_get_application_resources")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_get_application_resources(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                limit=limit,
                response_mode=response_mode,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_get_sync_history"]))
    async def argocd_get_sync_history(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_get_sync_history")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_get_sync_history(
                _argocd_client(), name=name, integration_id=integration_id, limit=limit
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_get_last_operation"]))
    async def argocd_get_last_operation(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_get_last_operation")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_get_last_operation(
                _argocd_client(), name=name, integration_id=integration_id
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_find_recent_deployments"]))
    async def argocd_find_recent_deployments(
        integration_id: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_find_recent_deployments")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_find_recent_deployments(
                _argocd_client(),
                integration_id=integration_id,
                project=project,
                namespace=namespace,
                limit=limit,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["argocd_analyze_application"]))
    async def argocd_analyze_application(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        history_limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("argocd_analyze_application")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _argocd_tools.argocd_analyze_application(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                response_mode=response_mode,
                history_limit=history_limit,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    def _grafana_client(workspace_id: str | None) -> PlatformGrafanaClient:
        _ = workspace_id
        resolved_workspace_id = _resolve_job_workspace_id(
            None,
            token_workspace_id=_current_token_workspace_id(),
            default_workspace_id=settings.mcp_default_workspace_id,
        )
        return PlatformGrafanaClient(settings, workspace_id=resolved_workspace_id)

    @mcp.tool(**_tool_metadata(_specs["grafana_list_dashboards"]))
    async def grafana_list_dashboards(workspace_id: str | None = None) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_list_dashboards")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_list_dashboards(_grafana_client(workspace_id))
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["grafana_get_dashboard"]))
    async def grafana_get_dashboard(
        dashboard_uid: str,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        panel_limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_get_dashboard")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_get_dashboard(
                _grafana_client(workspace_id),
                dashboard_uid=dashboard_uid,
                response_mode=response_mode,
                panel_limit=panel_limit,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["grafana_extract_panel_queries"]))
    async def grafana_extract_panel_queries(
        dashboard_uid: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_extract_panel_queries")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_extract_panel_queries(
                _grafana_client(workspace_id), dashboard_uid=dashboard_uid
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["grafana_metrics_query"]))
    async def grafana_metrics_query(
        datasource_uid: str,
        query: str,
        time: str | None = None,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        max_series: Annotated[int, Field(ge=1, le=100)] = 20,
        max_points: Annotated[int, Field(ge=1, le=1000)] = 120,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_metrics_query")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_metrics_query(
                _grafana_client(workspace_id),
                datasource_uid=datasource_uid,
                query=query,
                time=time,
                response_mode=response_mode,
                max_series=max_series,
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["grafana_metrics_query_range"]))
    async def grafana_metrics_query_range(
        datasource_uid: str,
        query: str,
        start: str,
        end: str,
        step: str,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        max_series: Annotated[int, Field(ge=1, le=100)] = 20,
        max_points: Annotated[int, Field(ge=1, le=1000)] = 120,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_metrics_query_range")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_metrics_query_range(
                _grafana_client(workspace_id),
                datasource_uid=datasource_uid,
                query=query,
                start=start,
                end=end,
                step=step,
                response_mode=response_mode,
                max_series=max_series,
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["analyze_dashboard_health"]))
    async def analyze_dashboard_health(
        dashboard_uid: str,
        start: str = "now-6h",
        end: str = "now",
        step: str | None = None,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        panel_limit: Annotated[int, Field(ge=1, le=50)] = 10,
        max_series: Annotated[int, Field(ge=1, le=100)] = 20,
        max_points: Annotated[int, Field(ge=1, le=1000)] = 120,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("analyze_dashboard_health")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.analyze_dashboard_health(
                _grafana_client(workspace_id),
                dashboard_uid=dashboard_uid,
                start=start,
                end=end,
                step=step,
                response_mode=response_mode,
                panel_limit=panel_limit,
                max_series=max_series,
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @mcp.tool(**_tool_metadata(_specs["grafana_get_panel_view"]))
    async def grafana_get_panel_view(
        dashboard_uid: str,
        panel_id: int,
        start: str = "now-1h",
        end: str = "now",
        variables: dict[str, str | list[str]] | None = None,
        max_points: Annotated[int, Field(ge=1, le=500)] = 300,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await _resolve_tool_guard("grafana_get_panel_view")
        if isinstance(guard, str):
            return _structured_guard_error(guard)
        try:
            result = await _grafana_tools.grafana_get_panel_view(
                _grafana_client(workspace_id),
                dashboard_uid=dashboard_uid,
                panel_id=panel_id,
                start=start,
                end=end,
                variables=variables or {},
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        panel_view = result.model_dump(mode="json")
        return {
            "structuredContent": panel_view,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f'Loaded Grafana panel "{panel_view["panel"]["title"]}" '
                        "for the selected time range."
                    ),
                }
            ],
            "_meta": {
                "datasourceUid": panel_view["source"].get("datasourceUid"),
                "rawPanelType": panel_view["panel"].get("type"),
            },
        }

    @mcp.tool(**_tool_metadata(_specs["k8s_describe_pod"]))
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
        guard = await _require_k8s_context("k8s_describe_pod")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}

        raw_str = await _send_k8s_agent_command(
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
            ctx = await _consult_pod_memory(describe, pod=pod, namespace=namespace)
            if ctx:
                describe["memory_context"] = ctx
        return _with_integration_context(
            describe,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    @mcp.tool(**_tool_metadata(_specs["k8s_debug_pod"]))
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
        guard = await _require_k8s_context("k8s_debug_pod")
        if isinstance(guard, str):
            return {"status": "failed", "error": guard}
        if not namespace:
            return {"status": "failed", "error": _MISSING_NAMESPACE_MESSAGE}

        describe_str, logs_raw_str = await asyncio.gather(
            _send_k8s_agent_command(
                settings=settings,
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
            ctx = await _consult_pod_memory(describe, pod=pod, namespace=namespace)
            if ctx:
                report["memory_context"] = ctx
        return _with_integration_context(
            report,
            guard if isinstance(guard, ResolvedIntegrationContext) else None,
            settings,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Knowledge write tool — private workspace semantic memory via Qdrant
    # ──────────────────────────────────────────────────────────────────────────
    from incidentflow_mcp.tools.knowledge_tools import knowledge_upsert
    from incidentflow_mcp.tools.memory_tools import MemoryAPIError

    def _workspace(workspace_id: str | None = None) -> str:
        wid = workspace_id or _current_token_workspace_id() or settings.mcp_default_workspace_id
        if not wid:
            raise ValueError(
                "workspace_id is required from auth context. For local development, set "
                "MCP_DEFAULT_WORKSPACE_ID."
            )
        return wid

    @mcp.tool(**_tool_metadata(_specs["knowledge_upsert"]))
    async def knowledge_upsert_tool(
        document_type: str,
        title: str,
        text: str,
        id: str | None = None,
        service: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        started_at: str | None = None,
        tags: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            return await knowledge_upsert(
                settings=settings,
                workspace_id=_workspace(),
                document_type=document_type,
                title=title,
                text=text,
                id=id,
                service=service,
                cluster=cluster,
                namespace=namespace,
                severity=severity,
                status=status,
                started_at=started_at,
                tags=tags,
                dry_run=dry_run,
            )
        except (MemoryAPIError, ValueError) as exc:
            return {"error": str(exc)}

    _harden_fastmcp_tool_contracts(mcp)
    register_resources(mcp)

    return mcp
