"""Strict structured contracts for FastMCP tools.

This module isolates the private FastMCP API touchpoints in one compatibility
boundary so SDK upgrades fail loudly in a focused place.

Two responsibilities:
1. Wrap every tool result in the canonical versioned response envelope
   (:mod:`incidentflow_mcp.tools.contracts`), mapping failures to canonical error
   codes and marking tool errors with ``isError=True``.
2. Register the precise inline output schema (envelope + per-tool data schema)
   with FastMCP so clients receive an exact ``outputSchema`` instead of a generic
   ``{additionalProperties: true}`` object.
"""

from __future__ import annotations

import json
from types import MethodType
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import CallToolResult, TextContent

from incidentflow_mcp.mcp.errors import (
    is_tool_error,
    map_exception,
    tool_error_fields,
)
from incidentflow_mcp.observability.tool_events import record_tool_failure
from incidentflow_mcp.tools.contracts import (
    ErrorCode,
    build_output_schema,
    error_envelope,
    generate_request_id,
    success_envelope,
)


class UnsupportedFastMCPVersionError(RuntimeError):
    """Raised when FastMCP internals no longer match the expected contract."""


def _error_result(tool_name: str, fields: dict[str, Any], request_id: str) -> CallToolResult:
    """Build an MCP CallToolResult carrying the error envelope with isError=True."""

    code: ErrorCode = fields["code"]
    envelope = error_envelope(
        tool_name=tool_name,
        code=code,
        message=str(fields.get("message") or ""),
        retryable=fields.get("retryable"),
        details=fields.get("details"),
        request_id=request_id,
    )
    # Surface the canonical code to structured logs / metrics (req #10): the same
    # value now lives in the response body, isError, logs and traces.
    record_tool_failure(
        error_code=code.value,
        error_type="MCPToolStructuredError",
        log_message=str(fields.get("message") or ""),
        retryable=bool(envelope["error"]["retryable"]),
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
        structuredContent=envelope,
        isError=True,
    )


async def run_tool_with_structured_errors(
    tool: Any,
    arguments: dict[str, Any],
    context: Any | None = None,
    convert_result: bool = False,
) -> Any:
    """Run a FastMCP tool and wrap the result in the canonical response envelope."""

    request_id = generate_request_id()
    try:
        result = await tool.fn_metadata.call_fn_with_arg_validation(
            tool.fn,
            tool.is_async,
            arguments,
            {tool.context_kwarg: context} if tool.context_kwarg is not None else None,
        )
    except UrlElicitationRequiredError:
        raise
    except Exception as exc:
        return _error_result(tool.name, map_exception(exc), request_id)

    # Inline tool-error signal returned by a handler (e.g. integration guard).
    if is_tool_error(result):
        return _error_result(tool.name, tool_error_fields(result), request_id)

    # Success: wrap the raw payload as `data`. Returned as a plain dict so the
    # lowlevel server validates it against the registered outputSchema.
    return success_envelope(result, tool_name=tool.name, request_id=request_id)


def harden_fastmcp_tool_contracts(mcp: FastMCP) -> None:
    """Make FastMCP argument validation strict, wrap results, and publish output schemas."""
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
        # Publish the precise inline envelope+data output schema to clients (req #5).
        tool.fn_metadata.output_schema = build_output_schema(tool.name)
        object.__setattr__(
            tool,
            "run",
            MethodType(run_tool_with_structured_errors, tool),
        )
