"""HTTP transport-level rate limiting middleware for MCP and auth endpoints."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.identity import IdentityResolver
from incidentflow_mcp.rate_limit.metrics import mcp_http_rate_limited_total, mcp_http_requests_total
from incidentflow_mcp.rate_limit.tool_guard import (
    build_transport_rate_limit_headers,
    parse_tool_call_payload,
    tool_guard_error_response,
)
from incidentflow_mcp.tools.contracts import ErrorCode

logger = logging.getLogger(__name__)


class TransportRateLimitMiddleware(BaseHTTPMiddleware):
    """Apply Redis-backed token-bucket limiting to selected HTTP endpoints."""

    def __init__(self, app, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._settings = settings
        self._identity = IdentityResolver()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._is_rate_limited_endpoint(request.url.path):
            return await call_next(request)

        policy = request.app.state.rate_limit_policy
        bucket_keys = request.app.state.rate_limit_bucket_keys
        store = request.app.state.rate_limit_store
        identity = self._identity.resolve(request)
        resolved_policy = policy.resolve(identity)
        transport_key = bucket_keys.transport_key(identity, resolved_policy)

        mcp_http_requests_total.inc()

        result = await store.take_token(
            scope=self._transport_scope_for_path(request.url.path),
            identity_key=transport_key,
            limit_per_min=resolved_policy.transport_limit_per_min,
        )
        if not result.allowed:
            mcp_http_rate_limited_total.inc()
            logger.warning(
                "rate_limit_hit type=http path=%s identity=%s policy=%s authenticated=%s",
                request.url.path,
                transport_key,
                resolved_policy.name,
                identity.authenticated,
            )
            headers = build_transport_rate_limit_headers(
                limit=result.limit,
                remaining=result.remaining,
                reset_after_ms=result.reset_after_ms,
            )
            # A tools/call caller gets the canonical RATE_LIMITED tool envelope, the
            # same shape as every other tool error; other requests keep HTTP 429.
            tool_call = await _extract_tool_call(request)
            if tool_call is not None:
                return tool_guard_error_response(
                    tool_call=tool_call,
                    code=ErrorCode.RATE_LIMITED,
                    reason="rate_limited",
                    limit_scope="transport",
                    message="Rate limit exceeded for MCP requests",
                    retry_after_seconds=int(headers["Retry-After"]),
                    headers=headers,
                )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too Many Requests"},
                headers=headers,
            )

        tool_call = await _extract_tool_call(request)
        if tool_call is None:
            return await call_next(request)

        return await request.app.state.tool_guard.guard(
            request=request,
            call_next=call_next,
            identity=identity,
            policy=resolved_policy,
            tool_call=tool_call,
        )

    def _is_rate_limited_endpoint(self, path: str) -> bool:
        if path == "/mcp":
            return True

        for endpoint in self._settings.rate_limited_auth_endpoints():
            if path == endpoint or path.startswith(f"{endpoint}/"):
                return True

        return False

    @staticmethod
    def _transport_scope_for_path(path: str) -> str:
        if path == "/mcp":
            return "http:mcp"
        return f"http:{path.strip('/').replace('/', ':')}"


async def _extract_tool_call(request: Request):
    if request.url.path != "/mcp" or request.method.upper() != "POST":
        return None

    try:
        payload = await request.json()
    except Exception:
        return None

    return parse_tool_call_payload(payload)
