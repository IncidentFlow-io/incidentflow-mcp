"""Strict structured contracts for FastMCP tools.

This module isolates the private FastMCP API touchpoints in one compatibility
boundary so SDK upgrades fail loudly in a focused place.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import UrlElicitationRequiredError

from incidentflow_mcp.mcp.errors import structured_tool_exception
from incidentflow_mcp.tools.contracts import apply_tool_contract


class UnsupportedFastMCPVersionError(RuntimeError):
    """Raised when FastMCP internals no longer match the expected contract."""


async def run_tool_with_structured_errors(
    tool: Any,
    arguments: dict[str, Any],
    context: Any | None = None,
    convert_result: bool = False,
) -> Any:
    """Run a FastMCP tool while preserving structured validation/runtime errors."""
    try:
        result = await tool.fn_metadata.call_fn_with_arg_validation(
            tool.fn,
            tool.is_async,
            arguments,
            {tool.context_kwarg: context} if tool.context_kwarg is not None else None,
        )
        if convert_result:
            result = tool.fn_metadata.convert_result(result)
        return apply_tool_contract(result, tool_name=tool.name)
    except UrlElicitationRequiredError:
        raise
    except Exception as exc:
        return apply_tool_contract(structured_tool_exception(exc), tool_name=tool.name)


def harden_fastmcp_tool_contracts(mcp: FastMCP) -> None:
    """Make FastMCP argument validation strict and keep validation errors structured."""
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None or not hasattr(tool_manager, "list_tools"):
        raise UnsupportedFastMCPVersionError(
            "Unsupported FastMCP version: _tool_manager.list_tools is unavailable; "
            "strict MCP tool contracts need a compatibility update."
        )
    for tool in tool_manager.list_tools():
        fn_metadata = getattr(tool, "fn_metadata", None)
        arg_model = getattr(fn_metadata, "arg_model", None)
        if fn_metadata is None or arg_model is None:
            raise UnsupportedFastMCPVersionError(
                f"Unsupported FastMCP tool metadata for {getattr(tool, 'name', '<unknown>')}; "
                "strict MCP tool contracts need a compatibility update."
            )
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)
        tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
        object.__setattr__(
            tool,
            "run",
            MethodType(run_tool_with_structured_errors, tool),
        )
