"""Request-local MCP tool outcome hints for structured logging."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_tool_event_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "mcp_tool_event_context",
    default=None,
)


def start_tool_event_context() -> Token[dict[str, Any] | None]:
    return _tool_event_context.set({})


def reset_tool_event_context(token: Token[dict[str, Any] | None]) -> None:
    _tool_event_context.reset(token)


def current_tool_event() -> dict[str, Any] | None:
    context = _tool_event_context.get()
    return dict(context) if context else None


def record_tool_rejection(**fields: Any) -> None:
    _record_tool_event(outcome="rejected", **fields)


def record_tool_failure(**fields: Any) -> None:
    _record_tool_event(outcome="failed", **fields)


def _record_tool_event(*, outcome: str, **fields: Any) -> None:
    context = _tool_event_context.get()
    if context is None:
        return
    context.clear()
    context.update(fields)
    context["outcome"] = outcome
