"""Request-scoped auth context shared between middleware and tool handlers."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TypedDict


class AuthContext(TypedDict):
    authenticated: bool
    client_id: str | None
    workspace_id: str | None
    user_id: str | None
    plan: str | None


_auth_context_var: ContextVar[AuthContext | None] = ContextVar(
    "incidentflow_mcp_auth_context",
    default=None,
)


def set_current_auth_context(context: AuthContext) -> None:
    _auth_context_var.set(context)


def get_current_auth_context() -> AuthContext | None:
    return _auth_context_var.get()


def clear_current_auth_context() -> None:
    _auth_context_var.set(None)
