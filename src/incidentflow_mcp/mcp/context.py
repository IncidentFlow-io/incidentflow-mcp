"""Shared registration context for MCP tool modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.auth.principal import IncidentFlowPrincipal, require_principal
from incidentflow_mcp.config import Settings
from incidentflow_mcp.integrations import (
    ResolvedIntegrationContext,
    resolve_tool_integration_context,
)
from incidentflow_mcp.tools.registry import ToolSpec, build_tool_description


@dataclass(frozen=True)
class ToolRegistrationContext:
    mcp: FastMCP
    settings: Settings
    specs: dict[str, ToolSpec]

    def metadata(self, tool_name: str) -> dict[str, Any]:
        spec = self.specs[tool_name]
        metadata = {
            "name": spec.name,
            "title": spec.title,
            "description": build_tool_description(
                spec,
                environment=self.settings.runtime_environment(),
            ),
            "annotations": spec.annotations,
        }
        if spec.meta:
            metadata["meta"] = spec.meta
        if spec.structured_output is not None:
            metadata["structured_output"] = spec.structured_output
        return metadata

    def principal(self) -> IncidentFlowPrincipal:
        return require_principal(get_current_auth_context(), settings=self.settings)

    async def resolve_tool_guard(
        self,
        tool_name: str,
    ) -> ResolvedIntegrationContext | str | None:
        return await resolve_tool_integration_context(
            tool=self.specs[tool_name],
            principal=self.principal(),
            settings=self.settings,
        )
