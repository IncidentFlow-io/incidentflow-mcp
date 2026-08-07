"""Registration for Knowledge MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_tool_exception
from incidentflow_mcp.tools.contracts import ErrorCode
from incidentflow_mcp.tools.integration_guide import integration_guide as _integration_guide
from incidentflow_mcp.tools.knowledge_search_tools import (
    KnowledgeSearchAPIError,
    knowledge_get,
    private_knowledge_search,
    public_knowledge_search,
)
from incidentflow_mcp.tools.knowledge_tools import knowledge_upsert
from incidentflow_mcp.tools.memory_tools import MemoryAPIError

TokenWorkspaceResolver = Callable[[], str | None]


def _normalize_search_response(payload: Any, *, query: str, scope: str) -> dict[str, Any]:
    """Adapter (review #9): surface a normalized {query, scope, results, total}
    view over the platform-api search payload while keeping upstream fields."""
    if not isinstance(payload, dict):
        return {"query": query, "scope": scope, "results": [], "total": 0}
    results = (
        payload.get("results")
        or (payload.get("publicResults") if scope == "public" else payload.get("workspaceResults"))
        or payload.get("items")
        or []
    )
    normalized = dict(payload)
    normalized.setdefault("query", query)
    normalized.setdefault("scope", scope)
    normalized["results"] = results if isinstance(results, list) else []
    if not isinstance(normalized.get("total"), int):
        normalized["total"] = len(normalized["results"])
    return normalized


def register_knowledge_tools(
    ctx: ToolRegistrationContext,
    *,
    current_token_workspace_id: TokenWorkspaceResolver,
) -> None:
    def _workspace(workspace_id: str | None = None) -> str:
        wid = workspace_id or current_token_workspace_id() or ctx.settings.mcp_default_workspace_id
        if not wid:
            raise ValueError(
                "workspace_id is required from auth context. For local development, set "
                "MCP_DEFAULT_WORKSPACE_ID."
            )
        return wid

    @ctx.mcp.tool(**ctx.metadata("public_knowledge_search"))
    async def public_knowledge_search_tool(
        query: str,
        document_type: str | None = None,
        response_mode: str = "compact",
        limit: int = 8,
    ) -> dict[str, Any]:
        try:
            payload = await public_knowledge_search(
                settings=ctx.settings,
                query=query,
                document_type=document_type,
                response_mode=response_mode,
                limit=limit,
            )
            return _normalize_search_response(payload, query=query, scope="public")
        except KnowledgeSearchAPIError as exc:
            return structured_tool_exception(exc, code=ErrorCode.UPSTREAM_ERROR)

    @ctx.mcp.tool(**ctx.metadata("integration_guide"))
    async def integration_guide_tool(
        integration: Literal["kubernetes", "slack", "grafana", "argocd"],
        goal: Literal["install", "configure", "verify", "upgrade", "troubleshoot", "uninstall"],
        method: str | None = None,
        environment: str | None = None,
        version: str | None = None,
        problem: str | None = None,
        context: dict[str, Any] | None = None,
        response_mode: Literal["compact", "full"] = "compact",
    ) -> dict[str, Any]:
        try:
            result = await _integration_guide(
                ctx.settings,
                integration=integration,
                goal=goal,
                method=method,
                environment=environment,
                version=version,
                problem=problem,
                context=context,
                response_mode=response_mode,
            )
        except KnowledgeSearchAPIError as exc:
            return structured_tool_exception(exc, code=ErrorCode.UPSTREAM_ERROR)
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("private_knowledge_search"))
    async def private_knowledge_search_tool(
        query: str,
        document_type: str | None = None,
        service: str | None = None,
        environment: str | None = None,
        response_mode: str = "compact",
        limit: int = 8,
    ) -> dict[str, Any]:
        try:
            payload = await private_knowledge_search(
                settings=ctx.settings,
                workspace_id=_workspace(),
                query=query,
                document_type=document_type,
                service=service,
                environment=environment,
                response_mode=response_mode,
                limit=limit,
            )
            return _normalize_search_response(payload, query=query, scope="workspace")
        except (KnowledgeSearchAPIError, ValueError) as exc:
            return structured_tool_exception(
                exc,
                code=ErrorCode.INVALID_ARGUMENT
                if isinstance(exc, ValueError)
                else ErrorCode.UPSTREAM_ERROR,
            )

    @ctx.mcp.tool(**ctx.metadata("knowledge_get"))
    async def knowledge_get_tool(
        id: str,
        id_type: str = "auto",
        document_type: str | None = None,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        try:
            return await knowledge_get(
                settings=ctx.settings,
                workspace_id=_workspace(),
                id=id,
                id_type=id_type,
                document_type=document_type,
                response_mode=response_mode,
            )
        except (KnowledgeSearchAPIError, ValueError) as exc:
            return structured_tool_exception(
                exc,
                code=ErrorCode.INVALID_ARGUMENT
                if isinstance(exc, ValueError)
                else ErrorCode.UPSTREAM_ERROR,
            )

    @ctx.mcp.tool(**ctx.metadata("knowledge_upsert"))
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
                settings=ctx.settings,
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
            return structured_tool_exception(
                exc,
                code=ErrorCode.INVALID_ARGUMENT
                if isinstance(exc, ValueError)
                else ErrorCode.UPSTREAM_ERROR,
            )
