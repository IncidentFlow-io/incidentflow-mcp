"""Per-call platform round-trips that are safe to cache briefly."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from incidentflow_mcp.auth import middleware
from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api import agent_commands_client
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient


def _settings(**changes: Any) -> Settings:
    return Settings(
        _env_file=None,
        platform_api_base_url="https://platform.invalid",
        platform_api_internal_api_key="internal-key",
        redis_url="redis://test-only",
        **changes,
    )


class _FakeAsyncClient:
    calls: ClassVar[list[tuple[str, str]]] = []
    responder: ClassVar[Any] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        type(self).calls.append(("GET", headers.get("Authorization", "")))
        return type(self).responder(httpx.Request("GET", url))

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        type(self).calls.append(("POST", kwargs["data"]["subject_token"]))
        return type(self).responder(httpx.Request("POST", url))


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncClient]:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(agent_commands_client.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(middleware.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


async def test_cluster_list_is_cached_per_bearer_and_copied(fake_http) -> None:
    fake_http.responder = lambda request: httpx.Response(
        200, request=request, json={"clusters": [{"cluster_id": "c1", "connected": True}]}
    )
    client = PlatformAPIAgentCommandsClient(_settings())

    first = await client.list_clusters(bearer_token="token-a")
    first[0]["connected"] = False  # caller mutation must not poison the cache
    second = await client.list_clusters(bearer_token="token-a")
    await client.list_clusters(bearer_token="token-b")

    assert second == [{"cluster_id": "c1", "connected": True}]
    assert fake_http.calls == [("GET", "Bearer token-a"), ("GET", "Bearer token-b")]


async def test_cluster_list_errors_are_not_cached(fake_http) -> None:
    fake_http.responder = lambda request: httpx.Response(503, request=request)
    client = PlatformAPIAgentCommandsClient(_settings())
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_clusters(bearer_token="token-a")
    assert len(fake_http.calls) == 2


async def test_token_exchange_is_reused_until_shortly_before_expiry(
    fake_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        mcp_platform_token_exchange_enabled=True,
        platform_api_token_exchange_api_key="exchange-key",
    )
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    fake_http.responder = lambda request: httpx.Response(
        200, request=request, json={"access_token": "platform-token", "expires_in": 300}
    )

    assert await middleware._platform_bearer_token("mcp-token") == "platform-token"
    assert await middleware._platform_bearer_token("mcp-token") == "platform-token"
    assert await middleware._platform_bearer_token("other-token") == "platform-token"
    assert fake_http.calls == [("POST", "mcp-token"), ("POST", "other-token")]

    # Expired (or about to): exchanged again.
    key = middleware._exchange_cache_key("mcp-token", settings.platform_api_token_exchange_scope)
    middleware._exchange_cache[key] = (0.0, "platform-token")
    await middleware._platform_bearer_token("mcp-token")
    assert fake_http.calls[-1] == ("POST", "mcp-token")


async def test_short_lived_or_failed_exchanges_are_not_cached(
    fake_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        mcp_platform_token_exchange_enabled=True,
        platform_api_token_exchange_api_key="exchange-key",
    )
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)

    fake_http.responder = lambda request: httpx.Response(
        200, request=request, json={"access_token": "t", "expires_in": 20}
    )
    await middleware._platform_bearer_token("mcp-token")
    await middleware._platform_bearer_token("mcp-token")
    assert len(fake_http.calls) == 2

    fake_http.responder = lambda request: httpx.Response(500, request=request)
    assert await middleware._platform_bearer_token("x") is None
    assert await middleware._platform_bearer_token("x") is None
    assert len(fake_http.calls) == 4
