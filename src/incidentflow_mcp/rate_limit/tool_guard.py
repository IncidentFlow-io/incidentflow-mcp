"""MCP tool invocation guard: rate limit, concurrency, timeout."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from incidentflow_mcp.observability.tool_events import GUARD_ERROR_SOURCE, record_tool_rejection
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.identity import ResolvedIdentity
from incidentflow_mcp.rate_limit.metrics import (
    mcp_tool_calls_total,
    mcp_tool_concurrency_rejections_total,
    mcp_tool_rate_limited_total,
    mcp_tool_timeouts_total,
)
from incidentflow_mcp.rate_limit.policy import PolicyResolver, RateLimitPolicy
from incidentflow_mcp.rate_limit.redis_store import RedisRateLimitStore
from incidentflow_mcp.tools.contracts import ErrorCode, error_envelope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPToolCall:
    request_id: object | None
    tool_name: str


class ToolInvocationGuard:
    def __init__(
        self,
        store: RedisRateLimitStore,
        policy_resolver: PolicyResolver,
        bucket_keys: BucketKeyResolver,
    ) -> None:
        self._store = store
        self._policy_resolver = policy_resolver
        self._bucket_keys = bucket_keys

    async def guard(
        self,
        *,
        request: Request,
        call_next,
        identity: ResolvedIdentity,
        policy: RateLimitPolicy,
        tool_call: MCPToolCall,
    ) -> Response:
        mcp_tool_calls_total.inc()
        tool_key = self._bucket_keys.tool_key(identity, policy)

        tool_result = await self._store.take_token(
            scope=f"tool:{tool_call.tool_name}",
            identity_key=tool_key,
            limit_per_min=policy.tool_limit_per_min,
        )
        if not tool_result.allowed:
            mcp_tool_rate_limited_total.inc()
            record_tool_rejection(
                reason="rate_limited",
                error_code="mcp_tool_rate_limited",
                error_type="MCPToolRateLimit",
                retryable=True,
            )
            logger.warning(
                "rate_limit_hit type=tool policy=%s tool=%s identity=%s",
                policy.name,
                tool_call.tool_name,
                tool_key,
            )
            return tool_guard_error_response(
                tool_call=tool_call,
                code=ErrorCode.RATE_LIMITED,
                reason="rate_limited",
                limit_scope="tool",
                message="Rate limit exceeded for tool invocation",
                retry_after_seconds=_retry_after_seconds(tool_result.reset_after_ms),
            )

        if self._policy_resolver.is_expensive_tool(tool_call.tool_name):
            expensive_result = await self._store.take_token(
                scope=f"tool-expensive:{tool_call.tool_name}",
                identity_key=tool_key,
                limit_per_min=policy.expensive_tool_limit_per_min,
            )
            if not expensive_result.allowed:
                mcp_tool_rate_limited_total.inc()
                record_tool_rejection(
                    reason="rate_limited",
                    error_code="mcp_tool_rate_limited",
                    error_type="MCPToolRateLimit",
                    retryable=True,
                )
                logger.warning(
                    "rate_limit_hit type=tool_expensive policy=%s tool=%s identity=%s",
                    policy.name,
                    tool_call.tool_name,
                    tool_key,
                )
                return tool_guard_error_response(
                    tool_call=tool_call,
                    code=ErrorCode.RATE_LIMITED,
                    reason="rate_limited",
                    limit_scope="tool_expensive",
                    message="Rate limit exceeded for tool invocation",
                    retry_after_seconds=_retry_after_seconds(expensive_result.reset_after_ms),
                )

        timeout_seconds = self._policy_resolver.resolve_tool_timeout_seconds(
            identity=identity,
            tool_name=tool_call.tool_name,
            policy=policy,
        )
        concurrency_key = self._bucket_keys.concurrency_key(identity, policy)
        concurrency_scope = f"tool-concurrency:{tool_call.tool_name}"
        concurrency_ttl_ms = max(60_000, (timeout_seconds + 5) * 1000)

        acquired = await self._store.acquire_concurrency(
            scope=concurrency_scope,
            identity_key=concurrency_key,
            limit=policy.concurrency_limit,
            ttl_ms=concurrency_ttl_ms,
        )
        if not acquired:
            mcp_tool_concurrency_rejections_total.inc()
            record_tool_rejection(
                reason="concurrency_limit",
                error_code="mcp_tool_concurrency_limit",
                error_type="MCPToolConcurrencyLimit",
                retryable=True,
            )
            logger.warning(
                "tool_concurrency_rejection policy=%s tool=%s identity=%s limit=%d",
                policy.name,
                tool_call.tool_name,
                concurrency_key,
                policy.concurrency_limit,
            )
            return tool_guard_error_response(
                tool_call=tool_call,
                code=ErrorCode.RATE_LIMITED,
                reason="concurrency_limit",
                limit_scope="concurrency",
                message="Too many concurrent tool invocations",
                retry_after_seconds=1,
            )

        try:
            return await asyncio.wait_for(call_next(request), timeout=timeout_seconds)
        except TimeoutError:
            mcp_tool_timeouts_total.inc()
            record_tool_rejection(
                reason="timeout",
                error_code="mcp_tool_timeout",
                error_type="MCPToolTimeout",
                retryable=True,
            )
            logger.warning(
                "tool_timeout policy=%s tool=%s identity=%s timeout_seconds=%d",
                policy.name,
                tool_call.tool_name,
                concurrency_key,
                timeout_seconds,
            )
            return tool_guard_error_response(
                tool_call=tool_call,
                code=ErrorCode.TIMEOUT,
                reason="timeout",
                limit_scope="timeout",
                message="Tool execution timed out",
                retry_after_seconds=None,
            )
        finally:
            await self._store.release_concurrency(
                scope=concurrency_scope,
                identity_key=concurrency_key,
            )


def parse_tool_call_payload(payload: Any) -> MCPToolCall | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("method") != "tools/call":
        return None

    params = payload.get("params")
    if not isinstance(params, dict):
        return None

    tool_name_raw = params.get("name")
    if not isinstance(tool_name_raw, str) or not tool_name_raw.strip():
        return None

    return MCPToolCall(request_id=payload.get("id"), tool_name=tool_name_raw.strip())


def build_transport_rate_limit_headers(
    *, limit: int, remaining: int, reset_after_ms: int
) -> dict[str, str]:
    retry_after_seconds = max(1, math.ceil(reset_after_ms / 1000))
    reset_epoch = int(time.time()) + retry_after_seconds
    return {
        "Retry-After": str(retry_after_seconds),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset": str(reset_epoch),
    }


def _retry_after_seconds(reset_after_ms: int) -> int:
    return max(1, math.ceil(reset_after_ms / 1000))


def tool_guard_error_response(
    *,
    tool_call: MCPToolCall,
    code: ErrorCode,
    reason: str,
    limit_scope: str,
    message: str,
    retry_after_seconds: int | None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Return a guard rejection as a normal ``tools/call`` error result.

    The client gets the same envelope as any other tool error
    (``structuredContent.error.code`` = RATE_LIMITED/TIMEOUT, ``retryable`` and
    ``details.retry_after_seconds``) instead of a JSON-RPC protocol error or a
    bare HTTP 429. ``Retry-After`` is also set as a header.
    """

    details: dict[str, Any] = {
        "source": GUARD_ERROR_SOURCE,
        "reason": reason,
        "limit_scope": limit_scope,
    }
    if retry_after_seconds is not None:
        details["retry_after_seconds"] = retry_after_seconds
    envelope = error_envelope(
        tool_name=tool_call.tool_name,
        code=code,
        message=message,
        retryable=True,
        details=details,
    )
    response_headers = dict(headers or {})
    if retry_after_seconds is not None:
        response_headers["Retry-After"] = str(retry_after_seconds)
    return JSONResponse(
        status_code=200,
        headers=response_headers,
        content={
            "jsonrpc": "2.0",
            "id": tool_call.request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(envelope, indent=2)}],
                "structuredContent": envelope,
                "isError": True,
            },
        },
    )
