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


_SCOPE_HINT = (
    "Narrow time_range, add scope (namespace, service, workload, pod, container), "
    "or add filters (contains, levels) and query again."
)


def _with_truncation(result: Any, requested_limit: int | None) -> Any:
    """Explain why a bounded log result was cut, without implying pagination.

    Logs queries are single-shot: platform-api never issues a cursor, so
    ``next_cursor`` is dropped and a truncated result carries a reason and a
    hint to narrow the query instead.
    """

    if not isinstance(result, dict) or "entries" not in result:
        return result
    result = dict(result)
    result.pop("next_cursor", None)
    if not result.get("truncated"):
        result["truncation"] = None
        return result
    returned = len(result.get("entries") or [])
    limits = result.get("applied_limits") or {}
    if requested_limit is None:
        reason = "result_limit"
        detail = f"Returned {returned} lines; the bounded context window has more lines."
    elif returned < requested_limit:
        reason = "byte_limit"
        detail = (
            f"Returned {returned} of up to {requested_limit} lines: the response hit the "
            f"source byte budget ({limits.get('max_bytes', 'unknown')} bytes)."
        )
    else:
        reason = "line_limit"
        cap = limits.get("max_lines")
        detail = f"Returned {returned} lines; more lines match this query."
        if isinstance(cap, int) and requested_limit < cap:
            detail += f" You can raise limit up to the source maximum of {cap}."
    result["truncation"] = {
        "reason": reason,
        "returned": returned,
        "detail": detail,
        "hint": _SCOPE_HINT + " Pagination is not supported.",
    }
    return result


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
            details = (
                {"retry_after_seconds": exc.retry_after_seconds}
                if exc.retry_after_seconds is not None
                else None
            )
            return tool_error(exc.code, exc.message, details=details)
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
        result = await call(
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
        return _with_truncation(result, limit)

    @ctx.mcp.tool(**ctx.metadata("logs_get_context"))
    async def logs_get_context(
        source_id: str, timestamp: datetime, scope: LogScope | None = None
    ) -> dict[str, Any]:
        result = await call(
            "logs_get_context",
            "context",
            {
                "source_id": source_id,
                "timestamp": timestamp.isoformat(),
                "scope": scope.model_dump(exclude_none=True) if scope else {},
            },
        )
        return _with_truncation(result, None)
