"""Workspace resolution policy for MCP tools."""

from __future__ import annotations

from dataclasses import dataclass

from incidentflow_mcp.mcp.request_context import MCPRequestContext
from incidentflow_mcp.mcp.services.async_jobs import resolve_job_workspace_id


@dataclass(frozen=True, slots=True)
class WorkspaceResolver:
    default_workspace_id: str | None
    request_context: MCPRequestContext

    def token_workspace_id(self) -> str | None:
        return self.request_context.workspace_id()

    def resolve(self, workspace_id: str | None = None) -> str:
        return resolve_job_workspace_id(
            workspace_id,
            token_workspace_id=self.token_workspace_id(),
            default_workspace_id=self.default_workspace_id,
        )

    def resolve_from_token(self, token_workspace_id: str | None) -> str:
        return resolve_job_workspace_id(
            None,
            token_workspace_id=token_workspace_id,
            default_workspace_id=self.default_workspace_id,
        )
