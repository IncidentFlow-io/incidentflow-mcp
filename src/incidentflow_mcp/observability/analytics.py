"""Fail-open publication of privacy-safe MCP product analytics events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.metrics import mcp_analytics_events_total

logger = logging.getLogger(__name__)


class ProductAnalyticsPublisher:
    """Bounded Redis Stream publisher, deliberately independent of tool results."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    async def start(self) -> None:
        if not self._settings.analytics_events_enabled:
            return
        self._client = from_url(self._settings.redis_url, encoding="utf-8", decode_responses=True)
        try:
            await asyncio.wait_for(
                self._client.ping(), timeout=self._settings.analytics_publish_timeout_ms / 1000
            )
        except (RedisError, TimeoutError):
            logger.exception("Analytics publisher unavailable at startup; MCP remains fail-open")
            await self._client.aclose()
            self._client = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish_mcp_tool(
        self,
        *,
        tool_name: str,
        outcome: str,
        duration_ms: int,
        request_id: object,
        auth_context: Mapping[str, object] | None,
        error_category: str | None = None,
    ) -> None:
        event = self._event(
            tool_name=tool_name,
            outcome=outcome,
            duration_ms=duration_ms,
            request_id=request_id,
            auth_context=auth_context,
            error_category=error_category,
        )
        if event is None:
            mcp_analytics_events_total.labels(event_name="mcp_tool", result="skipped").inc()
            return
        event_name = str(event["event_name"])
        if self._client is None:
            mcp_analytics_events_total.labels(event_name=event_name, result="unavailable").inc()
            return
        try:
            await asyncio.wait_for(
                self._client.xadd(
                    self._settings.analytics_stream_name,
                    {"payload": json.dumps(event, separators=(",", ":"))},
                    maxlen=self._settings.analytics_stream_maxlen,
                    approximate=True,
                ),
                timeout=self._settings.analytics_publish_timeout_ms / 1000,
            )
        except (RedisError, TimeoutError):
            mcp_analytics_events_total.labels(event_name=event_name, result="unavailable").inc()
            logger.warning(
                "Analytics event publication failed; MCP result is unaffected", exc_info=True
            )
            return
        mcp_analytics_events_total.labels(event_name=event_name, result="published").inc()

    def _event(
        self,
        *,
        tool_name: str,
        outcome: str,
        duration_ms: int,
        request_id: object,
        auth_context: Mapping[str, object] | None,
        error_category: str | None = None,
    ) -> dict[str, object] | None:
        if not auth_context or tool_name == "unknown":
            return None
        workspace_id = _uuid_string(auth_context.get("workspace_id"))
        user_id = _uuid_string(auth_context.get("user_id"))
        if workspace_id is None or user_id is None:
            return None
        succeeded = outcome == "success"
        request_id_value = str(request_id).strip() if request_id else None
        client_id = str(auth_context.get("client_id") or "").strip() or None
        return {
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(UTC).isoformat(),
            "event_name": "mcp_tool_completed" if succeeded else "mcp_tool_failed",
            "schema_version": 1,
            "source_service": "incidentflow-mcp",
            "workspace_id": workspace_id,
            "user_id": user_id,
            "tool_name": tool_name,
            "client_id": client_id[:128] if client_id else None,
            "outcome": "success" if succeeded else "failure",
            "duration_ms": duration_ms,
            "request_id": request_id_value[:128] if request_id_value else None,
            "traffic_class": (
                "customer"
                if self._settings.runtime_environment() == "production"
                or self._settings.analytics_emit_customer_events
                else "test"
            ),
            "properties": {
                "user_initiated": True,
                **({"error_category": error_category} if not succeeded and error_category else {}),
            },
        }


def _uuid_string(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None
