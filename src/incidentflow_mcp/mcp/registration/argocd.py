"""Registration for Argo CD MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_guard_error, structured_tool_exception
from incidentflow_mcp.platform_api.argocd_client import PlatformArgoCDClient
from incidentflow_mcp.tools import argocd as argocd_tools

WorkspaceResolver = Callable[[str | None], str]
TokenWorkspaceResolver = Callable[[], str | None]


def register_argocd_tools(
    ctx: ToolRegistrationContext,
    *,
    resolve_workspace_id: WorkspaceResolver,
    current_token_workspace_id: TokenWorkspaceResolver,
) -> None:
    def _argocd_client() -> PlatformArgoCDClient:
        resolved_workspace_id = resolve_workspace_id(current_token_workspace_id())
        return PlatformArgoCDClient(ctx.settings, workspace_id=resolved_workspace_id)

    @ctx.mcp.tool(**ctx.metadata("argocd_connection_health"))
    async def argocd_connection_health(integration_id: str | None = None) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_connection_health")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_connection_health(
                _argocd_client(), integration_id=integration_id
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_list_applications"))
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
        guard = await ctx.resolve_tool_guard("argocd_list_applications")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_list_applications(
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
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_get_application"))
    async def argocd_get_application(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        history_limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_get_application")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_get_application(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                response_mode=response_mode,
                history_limit=history_limit,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_get_application_resources"))
    async def argocd_get_application_resources(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        response_mode: Literal["compact", "full"] = "compact",
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_get_application_resources")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_get_application_resources(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                limit=limit,
                response_mode=response_mode,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_get_sync_history"))
    async def argocd_get_sync_history(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_get_sync_history")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_get_sync_history(
                _argocd_client(), name=name, integration_id=integration_id, limit=limit
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_get_last_operation"))
    async def argocd_get_last_operation(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_get_last_operation")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_get_last_operation(
                _argocd_client(), name=name, integration_id=integration_id
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_find_recent_deployments"))
    async def argocd_find_recent_deployments(
        integration_id: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_find_recent_deployments")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_find_recent_deployments(
                _argocd_client(),
                integration_id=integration_id,
                project=project,
                namespace=namespace,
                limit=limit,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("argocd_analyze_application"))
    async def argocd_analyze_application(
        name: Annotated[str, Field(min_length=1)],
        integration_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        history_limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("argocd_analyze_application")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await argocd_tools.argocd_analyze_application(
                _argocd_client(),
                name=name,
                integration_id=integration_id,
                response_mode=response_mode,
                history_limit=history_limit,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="ARGOCD_HTTP_ERROR")
        return result.model_dump(mode="json")
