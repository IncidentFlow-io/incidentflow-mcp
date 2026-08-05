"""Registration for Grafana MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_guard_error, structured_tool_exception
from incidentflow_mcp.platform_api.grafana_client import PlatformGrafanaClient
from incidentflow_mcp.tools import grafana as grafana_tools

WorkspaceResolver = Callable[[str | None], str]
TokenWorkspaceResolver = Callable[[], str | None]


def register_grafana_tools(
    ctx: ToolRegistrationContext,
    *,
    resolve_workspace_id: WorkspaceResolver,
    current_token_workspace_id: TokenWorkspaceResolver,
) -> None:
    def _grafana_client(workspace_id: str | None) -> PlatformGrafanaClient:
        _ = workspace_id
        resolved_workspace_id = resolve_workspace_id(current_token_workspace_id())
        return PlatformGrafanaClient(ctx.settings, workspace_id=resolved_workspace_id)

    @ctx.mcp.tool(**ctx.metadata("grafana_list_dashboards"))
    async def grafana_list_dashboards(workspace_id: str | None = None) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("grafana_list_dashboards")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_list_dashboards(_grafana_client(workspace_id))
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("grafana_get_dashboard"))
    async def grafana_get_dashboard(
        dashboard_uid: str,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        panel_limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("grafana_get_dashboard")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_get_dashboard(
                _grafana_client(workspace_id),
                dashboard_uid=dashboard_uid,
                response_mode=response_mode,
                panel_limit=panel_limit,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("grafana_extract_panel_queries"))
    async def grafana_extract_panel_queries(
        dashboard_uid: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("grafana_extract_panel_queries")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_extract_panel_queries(
                _grafana_client(workspace_id), dashboard_uid=dashboard_uid
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("grafana_metrics_query"))
    async def grafana_metrics_query(
        datasource_uid: str,
        query: str,
        time: str | None = None,
        workspace_id: str | None = None,
        response_mode: Literal["compact", "full"] = "compact",
        max_series: Annotated[int, Field(ge=1, le=100)] = 20,
        max_points: Annotated[int, Field(ge=1, le=1000)] = 120,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("grafana_metrics_query")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_metrics_query(
                _grafana_client(workspace_id),
                datasource_uid=datasource_uid,
                query=query,
                time=time,
                response_mode=response_mode,
                max_series=max_series,
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("grafana_metrics_query_range"))
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
        guard = await ctx.resolve_tool_guard("grafana_metrics_query_range")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_metrics_query_range(
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
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("analyze_dashboard_health"))
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
        guard = await ctx.resolve_tool_guard("analyze_dashboard_health")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.analyze_dashboard_health(
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
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("grafana_get_panel_view"))
    async def grafana_get_panel_view(
        dashboard_uid: str,
        panel_id: int,
        start: str = "now-1h",
        end: str = "now",
        variables: dict[str, str | list[str]] | None = None,
        max_points: Annotated[int, Field(ge=1, le=500)] = 300,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await ctx.resolve_tool_guard("grafana_get_panel_view")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            result = await grafana_tools.grafana_get_panel_view(
                _grafana_client(workspace_id),
                dashboard_uid=dashboard_uid,
                panel_id=panel_id,
                start=start,
                end=end,
                variables=variables or {},
                max_points=max_points,
            )
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")
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
