"""Registration for Knowledge MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.tools.knowledge_search_tools import (
    KnowledgeSearchAPIError,
    knowledge_get,
    private_knowledge_search,
    public_knowledge_search,
)
from incidentflow_mcp.tools.knowledge_tools import knowledge_upsert
from incidentflow_mcp.tools.memory_tools import MemoryAPIError

TokenWorkspaceResolver = Callable[[], str | None]


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
            return await public_knowledge_search(
                settings=ctx.settings,
                query=query,
                document_type=document_type,
                response_mode=response_mode,
                limit=limit,
            )
        except KnowledgeSearchAPIError as exc:
            return {"error": str(exc)}

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
            return await private_knowledge_search(
                settings=ctx.settings,
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
            return {"error": str(exc)}

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
            return {"error": str(exc)}
