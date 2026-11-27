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
from incidentflow_mcp.platform_api.entitlements_client import (
    EntitlementsUnavailable,
    PlatformEntitlementsClient,
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
        auth_context = get_current_auth_context() or {}
        workspace_id = str(auth_context.get("workspace_id") or "").strip()
        if not workspace_id:
            return _entitlement_error("ENTITLEMENTS_UNAVAILABLE", "Workspace context is required")
        spec = self.specs[tool_name]
        read_only = spec.annotations.get("readOnlyHint") is True
        try:
            # Reads may use the short-lived (<=60s) successful snapshot, as knowledge
            # tools already do; writes always re-check entitlements upstream.
            snapshot = await PlatformEntitlementsClient(self.settings).get(
                workspace_id, allow_cached=read_only
            )
        except EntitlementsUnavailable:
            return _entitlement_error(
                "ENTITLEMENTS_UNAVAILABLE", "Workspace entitlements are temporarily unavailable"
            )
        permitted = snapshot.readable if read_only else snapshot.writable
        if not permitted:
            return _entitlement_error(
                "BILLING_REMEDIATION_REQUIRED",
                (
                    "Read access is not available for this workspace"
                    if spec.annotations.get("readOnlyHint") is True
                    else "Billing needs attention before this workspace action can run"
                ),
            )
        return await resolve_tool_integration_context(
            tool=spec,
            principal=self.principal(),
            settings=self.settings,
        )


def _entitlement_error(code: str, message: str) -> str:
    return f'{{"code":"{code}","message":"{message}"}}'
