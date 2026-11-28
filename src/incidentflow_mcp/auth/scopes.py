"""MCP permission model: which scope a JSON-RPC request needs.

Streamable HTTP has a single endpoint (``/mcp``), so the required scope comes
from the JSON-RPC method (and, for ``tools/call``, the tool), never the URL.

Scopes form a hierarchy; a broader scope satisfies a narrower requirement:

    mcp:tools:write  ⇒  mcp:tools:run  ⇒  mcp:read

* ``mcp:read``        — protocol and discovery: ``initialize``, ``ping``,
                         ``tools/list``, ``resources/*``, ``prompts/*``, the
                         SSE ``GET`` stream, notifications.
* ``mcp:tools:run``   — ``tools/call`` for read-only tools.
* ``mcp:tools:write`` — ``tools/call`` for tools that change state
                         (``readOnlyHint`` is false in the registry).

There is no compatibility mode: a token without the required scope (or a
broader one) is refused with 403 ``insufficient_scope``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

SCOPE_READ = "mcp:read"
SCOPE_TOOLS_RUN = "mcp:tools:run"
SCOPE_TOOLS_WRITE = "mcp:tools:write"

MCP_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_TOOLS_RUN, SCOPE_TOOLS_WRITE)
# Scopes advertised to OAuth clients on the initial challenge: everything a
# normal assistant needs. Write access stays opt-in.
DEFAULT_REQUESTED_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_TOOLS_RUN)

# Each scope and the scopes it implies.
_IMPLIES: dict[str, frozenset[str]] = {
    SCOPE_READ: frozenset({SCOPE_READ}),
    SCOPE_TOOLS_RUN: frozenset({SCOPE_TOOLS_RUN, SCOPE_READ}),
    SCOPE_TOOLS_WRITE: frozenset({SCOPE_TOOLS_WRITE, SCOPE_TOOLS_RUN, SCOPE_READ}),
}
_RANK = {SCOPE_READ: 0, SCOPE_TOOLS_RUN: 1, SCOPE_TOOLS_WRITE: 2}

Decision = Literal["allow", "deny"]


@dataclass(frozen=True)
class ScopeRequirement:
    scope: str
    target: str  # e.g. "initialize" or "tools/call:knowledge_upsert", for logs and metrics


@lru_cache(maxsize=1)
def write_tool_names() -> frozenset[str]:
    from incidentflow_mcp.tools.registry import get_tool_specs

    return frozenset(
        spec.name for spec in get_tool_specs() if spec.annotations.get("readOnlyHint") is not True
    )


def _requirement_for_message(message: Any) -> ScopeRequirement:
    if not isinstance(message, dict):
        return ScopeRequirement(SCOPE_READ, "invalid")
    method = message.get("method")
    if method != "tools/call":
        return ScopeRequirement(SCOPE_READ, str(method or "response"))
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    tool = name.strip() if isinstance(name, str) else ""
    if tool in write_tool_names():
        return ScopeRequirement(SCOPE_TOOLS_WRITE, f"tools/call:{tool}")
    return ScopeRequirement(SCOPE_TOOLS_RUN, f"tools/call:{tool or 'unknown'}")


def requirement_for_payload(payload: Any) -> ScopeRequirement:
    """Strictest requirement across a JSON-RPC message or batch."""

    messages = payload if isinstance(payload, list) else [payload]
    requirements = [_requirement_for_message(message) for message in messages] or [
        ScopeRequirement(SCOPE_READ, "empty")
    ]
    return max(requirements, key=lambda item: _RANK[item.scope])


def effective_scopes(granted: Iterable[str]) -> frozenset[str]:
    expanded: set[str] = set()
    for scope in granted:
        expanded |= _IMPLIES.get(scope, frozenset({scope}))
    return frozenset(expanded)


def decide(granted: Iterable[str] | None, requirement: ScopeRequirement) -> Decision:
    """``granted=None`` is the local-only static PAT (refused in production): allowed."""

    if granted is None:
        return "allow"
    return "allow" if requirement.scope in effective_scopes(granted) else "deny"


def challenge_scope(requirement: ScopeRequirement | None) -> str:
    """Scope string for ``WWW-Authenticate``: what to request to proceed."""

    needed = set(DEFAULT_REQUESTED_SCOPES)
    if requirement is not None:
        needed.add(requirement.scope)
    return " ".join(sorted(needed, key=lambda scope: _RANK[scope]))
