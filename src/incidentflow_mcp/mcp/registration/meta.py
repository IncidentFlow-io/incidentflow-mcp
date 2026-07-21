"""Registration and payload builders for IncidentFlow MCP meta tools."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.auth.principal import IncidentFlowPrincipal
from incidentflow_mcp.config import Settings
from incidentflow_mcp.integrations import IntegrationStatusService, integration_actions
from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.tools.registry import get_tool_specs

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


def _checked_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
    categorized_names = {name for _, _, names in _CAPABILITY_CATEGORIES for name in names}
    unknown_tools = sorted(categorized_names - set(operational_specs))
    if unknown_tools:
        raise RuntimeError(f"Capability categories reference unknown tools: {unknown_tools}")

    categories = []
    emitted_categorized_names: set[str] = set()
    for category_id, label, names in _CAPABILITY_CATEGORIES:
        if category and category != category_id:
            emitted_categorized_names.update(names)
            continue
        tools = [
            _capability_tool_entry(operational_specs[name], response_mode=resolved_response_mode)
            for name in names
        ]
        emitted_categorized_names.update(names)
        categories.append(
            {
                "id": category_id,
                "label": label,
                "total": len(tools),
                "tools": tools,
            }
        )

    uncategorized = sorted(set(operational_specs) - emitted_categorized_names)
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


def register_meta_tools(ctx: ToolRegistrationContext) -> None:
    @ctx.mcp.tool(**ctx.metadata("incidentflow_capabilities"))
    async def incidentflow_capabilities(
        response_mode: str = "compact",
        category: str | None = None,
    ) -> dict[str, Any]:
        return _incidentflow_capabilities_payload(response_mode=response_mode, category=category)

    @ctx.mcp.tool(**ctx.metadata("mcp_version"))
    async def mcp_version() -> dict[str, Any]:
        return _mcp_version_payload(ctx.settings)

    @ctx.mcp.tool(**ctx.metadata("incidentflow_auth_status"))
    async def incidentflow_auth_status() -> dict[str, Any]:
        return await _incidentflow_auth_status_payload(
            settings=ctx.settings,
            principal=ctx.principal(),
        )

    @ctx.mcp.tool(**ctx.metadata("incidentflow_integrations_status"))
    async def incidentflow_integrations_status() -> dict[str, Any]:
        return await _incidentflow_integrations_status_payload(
            settings=ctx.settings,
            principal=ctx.principal(),
        )
