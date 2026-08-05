"""Shared MCP tool error payload helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError


def structured_guard_error(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "status": "failed", "error": raw}
    return decoded if isinstance(decoded, dict) else {"ok": False, "status": "failed", "error": raw}


def structured_tool_exception(exc: Exception, *, code: str = "TOOL_ERROR") -> dict[str, Any]:
    """Return a stable MCP tool error envelope for exceptions from upstream clients."""
    error: dict[str, Any] = {
        "code": code,
        "message": str(exc),
    }
    if isinstance(exc, ValidationError):
        error["code"] = "VALIDATION_ERROR"
        error["details"] = exc.errors()
    if isinstance(exc, httpx.HTTPStatusError):
        error["code"] = f"HTTP_{exc.response.status_code}"
        error["http_status"] = exc.response.status_code
        try:
            body = exc.response.json()
        except ValueError:
            body = exc.response.text
        if body:
            error["upstream_response"] = body
    return {"ok": False, "status": "failed", "warnings": [], "error": error}
