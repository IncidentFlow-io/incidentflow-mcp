"""Normalized request principal for MCP tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from incidentflow_mcp.auth.context import AuthContext
from incidentflow_mcp.config import Settings, get_settings

AuthMethod = Literal["oauth", "api_token", "unknown"]
RuntimeEnvironment = Literal["dev", "staging", "production"]


@dataclass(frozen=True)
class PrincipalUser:
    id: str
    email: str | None


@dataclass(frozen=True)
class PrincipalWorkspace:
    id: str
    name: str
    slug: str
    role: str


@dataclass(frozen=True)
class PrincipalRuntime:
    environment: RuntimeEnvironment


@dataclass(frozen=True)
class IncidentFlowPrincipal:
    authenticated: bool
    auth_method: AuthMethod
    user: PrincipalUser
    workspace: PrincipalWorkspace
    runtime: PrincipalRuntime


def _normalize_auth_method(value: object) -> AuthMethod:
    if value in {"oauth", "api_token"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _normalize_environment(settings: Settings) -> RuntimeEnvironment:
    raw = settings.runtime_environment()
    if raw in {"staging", "production"}:
        return raw  # type: ignore[return-value]
    return "dev"


def require_principal(
    request_context: AuthContext | None,
    *,
    settings: Settings | None = None,
) -> IncidentFlowPrincipal:
    """Build a safe principal from authenticated middleware context."""
    if not request_context or not request_context.get("authenticated"):
        raise ValueError("Authenticated MCP request context is required")

    selected_settings = settings or get_settings()
    workspace_id = str(
        request_context.get("workspace_id") or selected_settings.mcp_default_workspace_id or ""
    ).strip()
    if not workspace_id:
        raise ValueError("MCP request requires an authenticated workspace context")

    user_id = str(request_context.get("user_id") or request_context.get("client_id") or "").strip()
    if not user_id:
        user_id = "unknown"

    workspace_slug = str(request_context.get("workspace_slug") or "").strip()
    if not workspace_slug:
        workspace_slug = workspace_id

    workspace_name = str(request_context.get("workspace_name") or "").strip()
    if not workspace_name:
        workspace_name = workspace_slug

    workspace_role = str(request_context.get("workspace_role") or "").strip() or "unknown"

    return IncidentFlowPrincipal(
        authenticated=True,
        auth_method=_normalize_auth_method(request_context.get("auth_method")),
        user=PrincipalUser(
            id=user_id,
            email=request_context.get("email"),
        ),
        workspace=PrincipalWorkspace(
            id=workspace_id,
            name=workspace_name,
            slug=workspace_slug,
            role=workspace_role,
        ),
        runtime=PrincipalRuntime(environment=_normalize_environment(selected_settings)),
    )
