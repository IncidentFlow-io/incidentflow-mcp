"""Read-only vendor-neutral Logs MCP tools."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import (
    structured_guard_error,
    structured_tool_exception,
    tool_error,
)
from incidentflow_mcp.platform_api.logs_client import PlatformLogsClient, PlatformLogsError


class LogTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class LogScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: str | None = None
    environment: str | None = None
    namespace: str | None = None
    service: str | None = None
    workload: str | None = None
    pod: str | None = None
    container: str | None = None


class LogFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contains: list[str] = Field(default_factory=list, max_length=5)
    excludes: list[str] = Field(default_factory=list, max_length=5)
    levels: list[str] = Field(default_factory=list, max_length=8)
    labels: dict[str, str] = Field(default_factory=dict, max_length=32)


def register_logs_tools(
    ctx: ToolRegistrationContext, *, current_bearer_token, resolve_tool_guard
) -> None:
    def client() -> PlatformLogsClient:
        return PlatformLogsClient(
            ctx.settings,
            current_bearer_token(),
        )

    async def call(name: str, operation: str, payload: dict) -> Any:
        guard = await resolve_tool_guard(name)
        if isinstance(guard, str):
            return structured_guard_error(guard)
        try:
            upstream = client()
            if operation == "sources":
                return await upstream.list_sources()
            if operation == "health":
                return await upstream.health(payload["source_id"])
            if operation == "query":
                return await upstream.query(payload)
            return await upstream.context(payload)
        except PlatformLogsError as exc:
            return tool_error(exc.code, exc.message)
        except Exception:
            # Never copy upstream headers, URLs, credentials, or error bodies into MCP.
            return structured_tool_exception(
                RuntimeError("Logs request failed"), code="UPSTREAM_ERROR"
            )

    @ctx.mcp.tool(**ctx.metadata("logs_list_sources"))
    async def logs_list_sources() -> dict[str, Any]:
        result = await call("logs_list_sources", "sources", {})
        return result if isinstance(result, dict) else {"sources": result}

    @ctx.mcp.tool(**ctx.metadata("logs_connection_health"))
    async def logs_connection_health(source_id: str) -> dict[str, Any]:
        return await call("logs_connection_health", "health", {"source_id": source_id})

    @ctx.mcp.tool(**ctx.metadata("logs_query"))
    async def logs_query(
        source_id: str,
        time_range: LogTimeRange,
        scope: LogScope | None = None,
        filters: LogFilters | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        direction: Literal["forward", "backward"] = "backward",
    ) -> dict[str, Any]:
        return await call(
            "logs_query",
            "query",
            {
                "source_id": source_id,
                "time_range": time_range.model_dump(mode="json"),
                "scope": scope.model_dump(exclude_none=True) if scope else {},
                "filters": filters.model_dump() if filters else {},
                "limit": limit,
                "direction": direction,
            },
        )

    @ctx.mcp.tool(**ctx.metadata("logs_get_context"))
    async def logs_get_context(
        source_id: str, timestamp: datetime, scope: LogScope | None = None
    ) -> dict[str, Any]:
        return await call(
            "logs_get_context",
            "context",
            {
                "source_id": source_id,
                "timestamp": timestamp.isoformat(),
                "scope": scope.model_dump(exclude_none=True) if scope else {},
            },
        )
