import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.analytics import ProductAnalyticsPublisher
from incidentflow_mcp.observability.middleware import _analytics_error_category


def _settings(*, analytics_emit_customer_events: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        analytics_events_enabled=True,
        analytics_emit_customer_events=analytics_emit_customer_events,
        redis_url="redis://test-only",
    )


def _context() -> dict[str, object]:
    return {
        "workspace_id": str(uuid4()),
        "user_id": str(uuid4()),
        "client_id": "test-mcp-client",
    }


def test_event_uses_only_canonical_safe_fields() -> None:
    event = ProductAnalyticsPublisher(_settings())._event(
        tool_name="knowledge_upsert",
        outcome="success",
        duration_ms=12,
        request_id="request-123",
        auth_context=_context(),
    )

    assert event is not None
    assert event["event_name"] == "mcp_tool_completed"
    assert event["outcome"] == "success"
    assert event["properties"] == {"user_initiated": True}
    assert "arguments" not in event
    assert "result" not in event


def test_event_requires_uuid_user_and_workspace_attribution() -> None:
    event = ProductAnalyticsPublisher(_settings())._event(
        tool_name="knowledge_upsert",
        outcome="success",
        duration_ms=12,
        request_id="request-123",
        auth_context={"workspace_id": "workspace", "user_id": "user"},
    )

    assert event is None


def test_local_customer_analytics_requires_explicit_opt_in() -> None:
    event = ProductAnalyticsPublisher(_settings(analytics_emit_customer_events=True))._event(
        tool_name="knowledge_upsert",
        outcome="success",
        duration_ms=12,
        request_id="request-123",
        auth_context=_context(),
    )

    assert event is not None
    assert event["traffic_class"] == "customer"


@pytest.mark.asyncio
async def test_publish_uses_stream_and_never_exposes_tool_payload() -> None:
    publisher = ProductAnalyticsPublisher(_settings())
    client = AsyncMock()
    publisher._client = client

    await publisher.publish_mcp_tool(
        tool_name="knowledge_upsert",
        outcome="success",
        duration_ms=12,
        request_id="request-123",
        auth_context=_context(),
    )

    args = client.xadd.await_args.args
    body = json.loads(args[1]["payload"])
    assert args[0] == "incidentflow.analytics.events.v1"
    assert body["tool_name"] == "knowledge_upsert"
    assert body["properties"] == {"user_initiated": True}


@pytest.mark.asyncio
async def test_publish_failure_is_fail_open() -> None:
    publisher = ProductAnalyticsPublisher(_settings())
    publisher._client = AsyncMock()
    publisher._client.xadd.side_effect = RedisConnectionError("unavailable")

    await publisher.publish_mcp_tool(
        tool_name="knowledge_upsert",
        outcome="failure",
        duration_ms=12,
        request_id="request-123",
        auth_context=_context(),
    )


def test_failure_category_is_safe_and_does_not_guess_from_error_text() -> None:
    assert (
        _analytics_error_category(
            outcome="rejected", status_code=200, tool_event={"reason": "integration_missing"}
        )
        == "upstream"
    )
    assert (
        _analytics_error_category(outcome="error", status_code=429, tool_event=None) == "rate_limit"
    )
    assert (
        _analytics_error_category(
            outcome="error", status_code=200, tool_event={"log_message": "private secret"}
        )
        == "internal"
    )
    event = ProductAnalyticsPublisher(_settings())._event(
        tool_name="knowledge_upsert",
        outcome="failure",
        duration_ms=12,
        request_id="request-123",
        auth_context=_context(),
        error_category="upstream",
    )
    assert event is not None
    assert event["properties"] == {"user_initiated": True, "error_category": "upstream"}
