"""Redis store fallback behavior when backend is unavailable."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError

from incidentflow_mcp.rate_limit.redis_store import RedisRateLimitStore


class _FailingRedisClient:
    async def eval(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise ConnectionError("connection refused")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_take_token_fails_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incidentflow_mcp.rate_limit.redis_store.from_url", lambda *a, **k: _FailingRedisClient())
    store = RedisRateLimitStore("redis://example")

    result = await store.take_token(scope="http:mcp", identity_key="ip:127.0.0.1", limit_per_min=10)

    assert result.allowed is True
    assert result.remaining == 10
    assert result.reset_after_ms == 0


@pytest.mark.asyncio
async def test_concurrency_fails_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incidentflow_mcp.rate_limit.redis_store.from_url", lambda *a, **k: _FailingRedisClient())
    store = RedisRateLimitStore("redis://example")

    acquired = await store.acquire_concurrency(
        scope="tool-concurrency:incident_summary",
        identity_key="user:u1",
        limit=1,
        ttl_ms=60_000,
    )
    await store.release_concurrency(scope="tool-concurrency:incident_summary", identity_key="user:u1")

    assert acquired is True
