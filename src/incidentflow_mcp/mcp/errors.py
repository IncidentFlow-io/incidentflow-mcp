"""Canonical MCP tool error mapping.

Every tool failure resolves to exactly one :class:`ErrorCode`. Handlers that
detect a failure inline return :func:`tool_error` (a marked payload the contract
wrapper turns into an error envelope); uncaught exceptions are mapped by
:func:`map_exception`. The resolved code is the single value that appears in
``structuredContent.error.code``, the MCP ``isError`` outcome, logs, and metrics.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from incidentflow_mcp.tools.contracts import ErrorCode, default_retryable

# Marker key: a dict carrying this key is an inline tool-error signal, not data.
TOOL_ERROR_MARKER = "__tool_error__"


def tool_error(
    code: ErrorCode | str,
    message: str,
    *,
    retryable: bool | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Return a marked inline tool-error payload for a handler to ``return``."""

    resolved = code if isinstance(code, ErrorCode) else ErrorCode(str(code))
    return {
        TOOL_ERROR_MARKER: {
            "code": resolved,
            "message": message,
            "retryable": default_retryable(resolved) if retryable is None else bool(retryable),
            "details": details,
        }
    }


def is_tool_error(payload: Any) -> bool:
    """True when ``payload`` is a marked inline tool-error signal."""

    return isinstance(payload, dict) and TOOL_ERROR_MARKER in payload


def tool_error_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``{code, message, retryable, details}`` block from a marked payload."""

    return dict(payload[TOOL_ERROR_MARKER])


def _code_for_http_status(status_code: int) -> ErrorCode:
    if status_code == 400:
        return ErrorCode.INVALID_ARGUMENT
    if status_code == 401:
        return ErrorCode.UNAUTHENTICATED
    if status_code == 403:
        return ErrorCode.PERMISSION_DENIED
    if status_code == 404:
        return ErrorCode.NOT_FOUND
    if status_code == 409:
        return ErrorCode.CONFLICT
    if status_code == 429:
        return ErrorCode.RATE_LIMITED
    if 500 <= status_code < 600:
        return ErrorCode.UPSTREAM_ERROR
    if 400 <= status_code < 500:
        return ErrorCode.INVALID_ARGUMENT
    return ErrorCode.UPSTREAM_ERROR


def map_exception(exc: Exception) -> dict[str, Any]:
    """Map an exception to canonical error fields ``{code, message, retryable, details}``."""

    # pydantic input validation
    if isinstance(exc, ValidationError):
        return {
            "code": ErrorCode.INVALID_ARGUMENT,
            "message": "Invalid tool arguments",
            "retryable": False,
            "details": {"errors": exc.errors()},
        }

    # plain argument errors raised by handlers
    if isinstance(exc, (ValueError, TypeError)):
        return {
            "code": ErrorCode.INVALID_ARGUMENT,
            "message": str(exc),
            "retryable": False,
            "details": None,
        }

    # upstream HTTP errors from platform-api / integrations
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        code = _code_for_http_status(status_code)
        details: dict[str, Any] = {"http_status": status_code}
        try:
            body = exc.response.json()
        except ValueError:
            body = exc.response.text
        if body:
            details["upstream_response"] = body
        return {
            "code": code,
            "message": str(exc),
            "retryable": default_retryable(code),
            "details": details,
        }

    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return {
            "code": ErrorCode.TIMEOUT,
            "message": str(exc) or "Upstream request timed out",
            "retryable": True,
            "details": None,
        }

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
        return {
            "code": ErrorCode.INTEGRATION_UNAVAILABLE,
            "message": str(exc) or "Upstream integration is unavailable",
            "retryable": True,
            "details": None,
        }

    if isinstance(exc, httpx.HTTPError):
        return {
            "code": ErrorCode.UPSTREAM_ERROR,
            "message": str(exc),
            "retryable": True,
            "details": None,
        }

    return {
        "code": ErrorCode.INTERNAL_ERROR,
        "message": str(exc) or exc.__class__.__name__,
        "retryable": False,
        "details": None,
    }


def structured_guard_error(raw: str) -> dict[str, Any]:
    """Map an integration guard rejection to a canonical INTEGRATION_UNAVAILABLE error.

    ``raw`` is a JSON string (or plain text) produced by the integration guard
    describing why the tool cannot run (e.g. integration not connected).
    """

    message = raw
    details: Any = None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        message = str(decoded.get("message") or decoded.get("error") or raw)
        details = decoded
    return tool_error(
        ErrorCode.INTEGRATION_UNAVAILABLE,
        message,
        retryable=False,
        details=details,
    )


def structured_tool_exception(
    exc: Exception,
    *,
    code: ErrorCode | str | None = None,
) -> dict[str, Any]:
    """Map an exception to a marked inline tool-error payload.

    ``code`` overrides the inferred canonical code when a caller has more context.
    """

    fields = map_exception(exc)
    if code is not None:
        resolved = code if isinstance(code, ErrorCode) else ErrorCode(str(code))
        fields["code"] = resolved
        fields["retryable"] = default_retryable(resolved)
    return tool_error(
        fields["code"],
        fields["message"],
        retryable=fields["retryable"],
        details=fields.get("details"),
    )
