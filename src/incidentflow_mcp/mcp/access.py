"""Tool access resolution for MCP registrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from incidentflow_mcp.config import Settings
from incidentflow_mcp.integrations import (
    ResolvedIntegrationContext,
    resolve_tool_integration_context,
)
from incidentflow_mcp.mcp.request_context import MCPRequestContext
from incidentflow_mcp.tools.registry import ToolSpec

ResolveToolIntegrationContext = Callable[
    ...,
    Awaitable[ResolvedIntegrationContext | str | None],
]


@dataclass(frozen=True, slots=True)
class ToolAccessResolver:
    settings: Settings
    request_context: MCPRequestContext
    specs: Mapping[str, ToolSpec]
    integration_context_resolver: ResolveToolIntegrationContext = resolve_tool_integration_context

    async def resolve(self, tool_name: str) -> ResolvedIntegrationContext | str | None:
        try:
            spec = self.specs[tool_name]
        except KeyError as exc:
            raise RuntimeError(f"Tool specification is missing: {tool_name}") from exc

        return await self.integration_context_resolver(
            tool=spec,
            principal=self.request_context.principal(),
            settings=self.settings,
        )
