"""Integration status and guard helpers for IncidentFlow MCP tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.auth.principal import IncidentFlowPrincipal
from incidentflow_mcp.config import Settings
from incidentflow_mcp.logging_config import compact_log_fields
from incidentflow_mcp.observability.metrics import mcp_integration_guard_total
from incidentflow_mcp.observability.tool_events import record_tool_rejection
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient
from incidentflow_mcp.platform_api.integration_status_client import (
    IntegrationStatusEndpoint,
    PlatformIntegrationStatusClient,
)

logger = logging.getLogger(__name__)

IntegrationType = Literal["kubernetes", "grafana", "slack", "argocd"]
IntegrationConnectionStatus = Literal["connected", "not_connected", "degraded", "offline"]
IntegrationSource = Literal["workspace", "shared_dev"]


@dataclass(frozen=True)
class IntegrationStatus:
    type: IntegrationType
    status: IntegrationConnectionStatus
    source: IntegrationSource | None
    display_name: str
    resource_count: int | None = None
    message: str | None = None
    resource_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "source": self.source,
            "displayName": self.display_name,
        }
        if self.resource_count is not None:
            payload["resourceCount"] = self.resource_count
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True)
class ResolvedIntegrationContext:
    integration: IntegrationType
    source: IntegrationSource
    resource_id: str | None = None
    resource_name: str | None = None
    warning: str | None = None


class ToolDefinition(Protocol):
    name: str
    required_integration: IntegrationType | None
    supports_shared_dev_fallback: bool


class IntegrationStatusService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_statuses(
        self,
        principal: IncidentFlowPrincipal,
    ) -> dict[IntegrationType, IntegrationStatus]:
        statuses = await self._workspace_statuses(principal)
        if (
            statuses["kubernetes"].status != "connected"
            and self._settings.shared_dev_kubernetes_allowed()
        ):
            statuses["kubernetes"] = self._shared_dev_kubernetes_status()
        return statuses

    async def get_status(
        self,
        principal: IncidentFlowPrincipal,
        integration: IntegrationType,
    ) -> IntegrationStatus:
        return (await self.get_statuses(principal))[integration]

    async def _workspace_statuses(
        self,
        principal: IncidentFlowPrincipal,
    ) -> dict[IntegrationType, IntegrationStatus]:
        internal_statuses = await self._internal_workspace_statuses(principal)
        if internal_statuses is not None:
            return internal_statuses

        return {
            "kubernetes": await self._kubernetes_status(),
            "grafana": await self._grafana_status(),
            "slack": await self._slack_status(),
            "argocd": await self._argocd_status(),
        }

    async def _internal_workspace_statuses(
        self,
        principal: IncidentFlowPrincipal,
    ) -> dict[IntegrationType, IntegrationStatus] | None:
        if not (
            self._settings.platform_api_base_url and self._settings.platform_api_internal_api_key
        ):
            return None

        try:
            client = PlatformIntegrationStatusClient(self._settings)
            payload = await client.get_workspace_status(workspace_id=principal.workspace.id)
        except httpx.HTTPStatusError as exc:
            # The endpoint is new; keep local development compatible with older platform-api.
            if exc.response.status_code == 404:
                return None
            logger.warning(
                "integration_status_request_failed",
                extra=compact_log_fields(
                    integration="all",
                    workspace_id=principal.workspace.id,
                    operation="get_workspace_integration_status",
                    upstream_service="platform-api",
                    upstream_route="/internal/integrations/status/workspace",
                    upstream_status=exc.response.status_code,
                    error_code="platform_api_internal_error",
                    error_type=type(exc).__name__,
                    retryable=exc.response.status_code >= 500,
                    log_message=(
                        "Failed to fetch workspace integration status; "
                        "using fallback status checks."
                    ),
                ),
            )
            return None
        except (ValueError, httpx.HTTPError) as exc:
            logger.warning(
                "integration_status_request_unavailable",
                extra=compact_log_fields(
                    integration="all",
                    workspace_id=principal.workspace.id,
                    operation="get_workspace_integration_status",
                    upstream_service="platform-api",
                    upstream_route="/internal/integrations/status/workspace",
                    error_code="platform_api_unavailable",
                    error_type=type(exc).__name__,
                    retryable=True,
                    log_message=(
                        "Workspace integration status is unavailable; using fallback status checks."
                    ),
                ),
            )
            return None

        return {
            "kubernetes": self._kubernetes_status_from_payload(payload.get("kubernetes")),
            "grafana": self._integration_status_from_payload(
                "grafana",
                payload.get("grafana"),
                display_name="Grafana",
                not_connected_message="Grafana is not connected for the current workspace.",
                resource_count=lambda item: len(item.get("datasources") or []),
            ),
            "slack": self._integration_status_from_payload(
                "slack",
                payload.get("slack"),
                display_name="Slack",
                not_connected_message="Slack is not connected for the current workspace.",
                payload_display_name=lambda item: _optional_str(item.get("workspace_name")),
            ),
            "argocd": self._integration_status_from_payload(
                "argocd",
                payload.get("argocd"),
                display_name="Argo CD",
                not_connected_message="Argo CD is not connected for the current workspace.",
                resource_count=lambda item: _first_int(item, "application_count"),
                resource_id=lambda item: _optional_str(item.get("id")),
                payload_display_name=lambda item: _optional_str(item.get("display_name")),
            ),
        }

    def _current_bearer_token(self) -> str:
        context = get_current_auth_context()
        return str((context or {}).get("bearer_token") or "").strip()

    async def _kubernetes_status(self) -> IntegrationStatus:
        bearer_token = self._current_bearer_token()
        if not self._settings.platform_api_base_url or not bearer_token:
            return IntegrationStatus(
                type="kubernetes",
                status="not_connected",
                source=None,
                display_name="Kubernetes",
                message="Kubernetes is not connected for the current workspace.",
            )

        try:
            client = PlatformAPIAgentCommandsClient(self._settings)
            clusters = await client.list_clusters(bearer_token=bearer_token)
        except httpx.HTTPError:
            return IntegrationStatus(
                type="kubernetes",
                status="offline",
                source="workspace",
                display_name="Kubernetes",
                message="Kubernetes agent lookup is temporarily unavailable.",
            )

        connected = [item for item in clusters if item.get("connected") is True]
        if not connected:
            return IntegrationStatus(
                type="kubernetes",
                status="not_connected",
                source=None,
                display_name="Kubernetes",
                resource_count=len(clusters),
                message="Kubernetes is not connected for the current workspace.",
            )

        cluster = connected[0]
        return IntegrationStatus(
            type="kubernetes",
            status="connected",
            source="workspace",
            display_name=str(cluster.get("name") or "Kubernetes"),
            resource_count=len(connected),
            resource_id=str(cluster.get("cluster_id") or ""),
        )

    def _kubernetes_status_from_payload(self, raw: object) -> IntegrationStatus:
        payload = raw if isinstance(raw, dict) else {}
        raw_clusters = payload.get("clusters")
        clusters = raw_clusters if isinstance(raw_clusters, list) else []
        connected = [
            item for item in clusters if isinstance(item, dict) and item.get("connected") is True
        ]
        if not connected:
            return IntegrationStatus(
                type="kubernetes",
                status="not_connected",
                source=None,
                display_name="Kubernetes",
                resource_count=len(clusters),
                message="Kubernetes is not connected for the current workspace.",
            )

        cluster = connected[0]
        return IntegrationStatus(
            type="kubernetes",
            status="connected",
            source="workspace",
            display_name=str(cluster.get("name") or "Kubernetes"),
            resource_count=len(connected),
            resource_id=str(cluster.get("cluster_id") or ""),
        )

    def _shared_dev_kubernetes_status(self) -> IntegrationStatus:
        return IntegrationStatus(
            type="kubernetes",
            status="connected",
            source="shared_dev",
            display_name=self._settings.shared_dev_kubernetes_cluster_name,
            resource_count=1,
            resource_id=self._settings.shared_dev_kubernetes_agent_id,
            message="Using the shared IncidentFlow development Kubernetes agent.",
        )

    async def _platform_status(
        self,
        integration: IntegrationStatusEndpoint,
        *,
        display_name: str,
        not_connected_message: str,
        offline_message: str,
        resource_count: Callable[[dict[str, Any]], int | None] | None = None,
        resource_id: Callable[[dict[str, Any]], str | None] | None = None,
        payload_display_name: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> IntegrationStatus:
        bearer_token = self._current_bearer_token()
        if not self._settings.platform_api_base_url or not bearer_token:
            return IntegrationStatus(
                type=integration,
                status="not_connected",
                source=None,
                display_name=display_name,
                message=not_connected_message,
            )

        try:
            client = PlatformIntegrationStatusClient(self._settings)
            payload = await client.get_status(integration, bearer_token=bearer_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 404}:
                return IntegrationStatus(
                    type=integration,
                    status="not_connected",
                    source=None,
                    display_name=display_name,
                    message=not_connected_message,
                )
            return IntegrationStatus(
                type=integration,
                status="offline",
                source="workspace",
                display_name=display_name,
                message=offline_message,
            )
        except httpx.HTTPError:
            return IntegrationStatus(
                type=integration,
                status="offline",
                source="workspace",
                display_name=display_name,
                message=offline_message,
            )

        connected = payload.get("connected") is True
        resolved_display_name = (
            payload_display_name(payload) if payload_display_name is not None else None
        )
        resolved_display_name = (resolved_display_name or display_name).strip()
        return IntegrationStatus(
            type=integration,
            status="connected" if connected else "not_connected",
            source="workspace" if connected else None,
            display_name=resolved_display_name,
            resource_count=resource_count(payload) if resource_count else None,
            resource_id=resource_id(payload) if resource_id else None,
            message=(
                None if connected else str(payload.get("error_message") or not_connected_message)
            ),
        )

    def _integration_status_from_payload(
        self,
        integration: IntegrationStatusEndpoint,
        raw: object,
        *,
        display_name: str,
        not_connected_message: str,
        resource_count: Callable[[dict[str, Any]], int | None] | None = None,
        resource_id: Callable[[dict[str, Any]], str | None] | None = None,
        payload_display_name: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> IntegrationStatus:
        payload = raw if isinstance(raw, dict) else {}
        connected = payload.get("connected") is True
        resolved_display_name = (
            payload_display_name(payload) if payload_display_name is not None else None
        )
        return IntegrationStatus(
            type=integration,
            status="connected" if connected else "not_connected",
            source="workspace" if connected else None,
            display_name=(resolved_display_name or display_name).strip(),
            resource_count=resource_count(payload) if resource_count else None,
            resource_id=resource_id(payload) if resource_id else None,
            message=(
                None if connected else str(payload.get("error_message") or not_connected_message)
            ),
        )

    async def _grafana_status(self) -> IntegrationStatus:
        return await self._platform_status(
            "grafana",
            display_name="Grafana",
            not_connected_message="Grafana is not connected for the current workspace.",
            offline_message="Grafana status lookup is temporarily unavailable.",
            resource_count=lambda payload: len(payload.get("datasources") or []),
        )

    async def _slack_status(self) -> IntegrationStatus:
        return await self._platform_status(
            "slack",
            display_name="Slack",
            not_connected_message="Slack is not connected for the current workspace.",
            offline_message="Slack status lookup is temporarily unavailable.",
            payload_display_name=lambda payload: str(payload.get("workspace_name") or "Slack"),
        )

    async def _argocd_status(self) -> IntegrationStatus:
        return await self._platform_status(
            "argocd",
            display_name="Argo CD",
            not_connected_message="Argo CD is not connected for the current workspace.",
            offline_message="Argo CD status lookup is temporarily unavailable.",
            resource_count=lambda payload: _first_int(payload, "application_count"),
            resource_id=lambda payload: _optional_str(payload.get("id")),
            payload_display_name=lambda payload: _optional_str(payload.get("display_name")),
        )


def integration_display_name(integration: IntegrationType) -> str:
    return {
        "kubernetes": "Kubernetes",
        "grafana": "Grafana",
        "slack": "Slack",
        "argocd": "Argo CD",
    }[integration]


def integration_actions(integration: IntegrationType, settings: Settings) -> list[dict[str, str]]:
    app_base_url = {
        "dev": "https://app-dev.incidentflow.io",
        "staging": "https://app-staging.incidentflow.io",
        "production": "https://app.incidentflow.io",
    }.get(settings.runtime_environment(), "https://app-dev.incidentflow.io")
    label = integration_display_name(integration)
    return [
        {
            "type": "open_url",
            "label": f"Connect {label}",
            "url": f"{app_base_url}/integrations",
        },
        {
            "type": "open_url",
            "label": "Read setup guide",
            "url": f"https://incidentflow.io/docs/integrations/{integration}",
        },
    ]


def integration_required(integration: IntegrationType, settings: Settings) -> str:
    label = integration_display_name(integration)
    return json.dumps(
        {
            "ok": False,
            "code": "INTEGRATION_NOT_CONNECTED",
            "status": "not_connected",
            "integration": integration,
            "message": f"{label} is not connected for the current workspace.",
            "actions": integration_actions(integration, settings),
        },
        indent=2,
    )


async def resolve_tool_integration_context(
    *,
    tool: ToolDefinition,
    principal: IncidentFlowPrincipal,
    settings: Settings,
    service: IntegrationStatusService | None = None,
) -> ResolvedIntegrationContext | str | None:
    integration = tool.required_integration
    if not integration:
        return None

    status_service = service or IntegrationStatusService(settings)
    status = await status_service.get_status(principal, integration)
    if status.status == "connected" and status.source == "workspace":
        _record_guard_metric(
            tool=tool,
            integration=integration,
            result="workspace",
            principal=principal,
        )
        logger.info(
            "mcp_tool_integration_resolved",
            extra=compact_log_fields(
                tool_name=tool.name,
                workspace_id=principal.workspace.id,
                integration=integration,
                source=status.source,
                requested_environment=principal.runtime.environment,
            ),
        )
        return ResolvedIntegrationContext(
            integration=integration,
            source="workspace",
            resource_id=status.resource_id,
            resource_name=status.display_name,
        )

    if (
        integration == "kubernetes"
        and principal.runtime.environment == "dev"
        and tool.supports_shared_dev_fallback
        and settings.shared_dev_kubernetes_allowed()
    ):
        _record_guard_metric(
            tool=tool,
            integration=integration,
            result="shared_dev",
            principal=principal,
        )
        logger.info(
            "mcp_tool_integration_resolved",
            extra=compact_log_fields(
                tool_name=tool.name,
                workspace_id=principal.workspace.id,
                integration=integration,
                source="shared_dev",
                requested_environment=principal.runtime.environment,
            ),
        )
        return ResolvedIntegrationContext(
            integration=integration,
            source="shared_dev",
            resource_id=settings.shared_dev_kubernetes_agent_id,
            resource_name=settings.shared_dev_kubernetes_cluster_name,
            warning="Using the shared IncidentFlow development Kubernetes agent.",
        )

    _record_guard_metric(
        tool=tool,
        integration=integration,
        result="not_connected",
        principal=principal,
    )
    record_tool_rejection(
        reason="integration_missing",
        integration=integration,
        requested_environment=principal.runtime.environment,
        remediation=f"connect_{integration}_integration",
    )
    logger.debug(
        "mcp_tool_integration_missing",
        extra=compact_log_fields(
            tool_name=tool.name,
            workspace_id=principal.workspace.id,
            integration=integration,
            requested_environment=principal.runtime.environment,
        ),
    )
    return integration_required(integration, settings)


def _record_guard_metric(
    *,
    tool: ToolDefinition,
    integration: IntegrationType,
    result: str,
    principal: IncidentFlowPrincipal,
) -> None:
    mcp_integration_guard_total.labels(
        tool=tool.name,
        integration=integration,
        result=result,
        environment=principal.runtime.environment,
    ).inc()


def attach_integration_context(
    raw_result: str,
    context: ResolvedIntegrationContext | None,
    settings: Settings,
) -> str:
    if context is None or context.source != "shared_dev":
        return raw_result
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result
    if not isinstance(payload, dict):
        return raw_result

    payload.setdefault(
        "connection",
        {
            "source": "shared_dev_agent",
            "cluster": context.resource_name or settings.shared_dev_kubernetes_cluster_name,
            "environment": settings.runtime_environment(),
        },
    )
    payload.setdefault("warnings", [])
    if context.warning and context.warning not in payload["warnings"]:
        payload["warnings"].append(context.warning)
    return json.dumps(payload, indent=2)


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        try:
            value = payload.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
