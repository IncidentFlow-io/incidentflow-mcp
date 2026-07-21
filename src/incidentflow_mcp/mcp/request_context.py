"""Request-scoped MCP auth context helpers."""

from __future__ import annotations

from dataclasses import dataclass

from incidentflow_mcp.auth.context import get_current_auth_context
from incidentflow_mcp.auth.principal import IncidentFlowPrincipal, require_principal
from incidentflow_mcp.config import Settings


class MissingAuthenticationError(RuntimeError):
    """Raised when a tool requires authenticated MCP request context."""


@dataclass(frozen=True, slots=True)
class MCPRequestContext:
    settings: Settings

    def principal(self) -> IncidentFlowPrincipal:
        return require_principal(get_current_auth_context(), settings=self.settings)

    def workspace_id(self) -> str | None:
        auth_context = get_current_auth_context()
        if not auth_context:
            return None
        workspace_id = auth_context.get("workspace_id")
        if workspace_id is None:
            return None
        normalized = str(workspace_id).strip()
        return normalized or None

    def bearer_token(self) -> str:
        auth_context = get_current_auth_context()
        if not auth_context:
            raise MissingAuthenticationError("Authenticated MCP request context is required")
        token = str(auth_context.get("bearer_token") or "").strip()
        if not token:
            raise MissingAuthenticationError("Bearer token is required for Kubernetes agent tools")
        return token
