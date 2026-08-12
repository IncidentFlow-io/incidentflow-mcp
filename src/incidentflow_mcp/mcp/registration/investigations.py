"""MCP control surface for durable Investigation lifecycle passes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import httpx
from pydantic import Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_tool_exception
from incidentflow_mcp.platform_api.investigations_client import PlatformInvestigationsClient
from incidentflow_mcp.tools import investigations as investigation_tools


def register_investigation_tools(
    ctx: ToolRegistrationContext, *, current_bearer_token: Callable[[], str]
) -> None:
    def client() -> PlatformInvestigationsClient:
        return PlatformInvestigationsClient(ctx.settings, bearer_token=current_bearer_token())

    @ctx.mcp.tool(**ctx.metadata("investigation_list"))
    async def investigation_list(
        needs_attention: bool | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 20
    ) -> dict[str, Any]:
        try:
            return (
                await investigation_tools.investigation_list(
                    client(), needs_attention=needs_attention, limit=limit
                )
            ).model_dump(mode="json")
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc)

    @ctx.mcp.tool(**ctx.metadata("investigation_get"))
    async def investigation_get(
        investigation_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        try:
            return (
                await investigation_tools.investigation_get(
                    client(), investigation_id=investigation_id
                )
            ).model_dump(mode="json")
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc)

    @ctx.mcp.tool(**ctx.metadata("investigation_continue"))
    async def investigation_continue(
        investigation_id: Annotated[str, Field(min_length=1)],
        reason: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> dict[str, Any]:
        try:
            return (
                await investigation_tools.investigation_continue(
                    client(), investigation_id=investigation_id, reason=reason
                )
            ).model_dump(mode="json")
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc)

    @ctx.mcp.tool(**ctx.metadata("investigation_recheck"))
    async def investigation_recheck(
        investigation_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        try:
            return (
                await investigation_tools.investigation_recheck(
                    client(), investigation_id=investigation_id
                )
            ).model_dump(mode="json")
        except httpx.HTTPStatusError as exc:
            return structured_tool_exception(exc)
