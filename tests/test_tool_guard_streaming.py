"""The tool guard must cover the whole SSE response, not only time-to-headers.

MCP Streamable HTTP answers ``tools/call`` with ``text/event-stream``; Starlette's
``call_next`` returns once headers are sent, while the tool keeps running.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository
from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.identity import ResolvedIdentity
from incidentflow_mcp.rate_limit.policy import DefaultPolicyResolver
from incidentflow_mcp.rate_limit.tool_guard import MCPToolCall, ToolInvocationGuard

_IDENTITY = ResolvedIdentity(
    authenticated=True,
    ip_address="127.0.0.1",
    workspace_id="w1",
    user_id="u1",
    client_id="c1",
    plan="any",
)
_RESULT_EVENT = b'event: message\ndata: {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}\n\n'


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "scheme": "http",
        "http_version": "1.1",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _guard(store, *, timeout_seconds: int = 30, concurrency: int = 1):
    settings = Settings(
        _env_file=None,
        redis_url="redis://test-only",
        platform_api_base_url=None,
        tool_concurrency_authenticated=concurrency,
        tool_limit_authenticated_per_min=100,
        tool_timeout_seconds=timeout_seconds,
        expensive_tools="",
    )
    resolver = DefaultPolicyResolver(settings)
    guard = ToolInvocationGuard(store, resolver, BucketKeyResolver())
    return guard, resolver.resolve(_IDENTITY)


def _sse(*chunks: bytes, delay: float = 0.0):
    async def call_next(_: Request) -> StreamingResponse:
        async def body():
            for chunk in chunks:
                await asyncio.sleep(delay)
                yield chunk

        return StreamingResponse(body(), media_type="text/event-stream")

    return call_next


async def _drain(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


async def _call(guard, policy, call_next, request_id: int = 1):
    return await guard.guard(
        request=_request(),
        call_next=call_next,
        identity=_IDENTITY,
        policy=policy,
        tool_call=MCPToolCall(request_id=request_id, tool_name="k8s_list_pods"),
    )


def _envelope(body: bytes) -> dict:
    return json.loads(body)["result"]["structuredContent"]


async def test_concurrency_slot_is_held_until_the_sse_body_finishes(rate_limit_store) -> None:
    guard, policy = _guard(rate_limit_store, concurrency=1)

    first = await _call(guard, policy, _sse(_RESULT_EVENT))
    # Headers are out but the body has not been relayed: the tool is still running.
    rejected = await _call(guard, policy, _sse(_RESULT_EVENT), request_id=2)
    assert _envelope(rejected.body)["error"]["details"]["reason"] == "concurrency_limit"

    assert await _drain(first) == _RESULT_EVENT
    after = await _call(guard, policy, _sse(_RESULT_EVENT), request_id=3)
    assert await _drain(after) == _RESULT_EVENT


async def test_deadline_applies_to_the_sse_body_and_emits_timeout_envelope(
    rate_limit_store,
) -> None:
    guard, policy = _guard(rate_limit_store, timeout_seconds=1)

    response = await _call(guard, policy, _sse(_RESULT_EVENT, delay=1.5))
    body = await _drain(response)

    assert body.startswith(b"event: message\ndata: ")
    message = json.loads(body.split(b"data: ", 1)[1])
    assert message["id"] == 1
    envelope = message["result"]["structuredContent"]
    assert envelope["error"]["code"] == "TIMEOUT"
    assert envelope["error"]["retryable"] is True
    assert envelope["error"]["details"]["source"] == "mcp_guard"
    # The slot was released after the timeout.
    follow_up = await _call(guard, policy, _sse(_RESULT_EVENT), request_id=2)
    assert await _drain(follow_up) == _RESULT_EVENT


async def test_no_timeout_event_after_the_response_was_relayed(rate_limit_store) -> None:
    guard, policy = _guard(rate_limit_store, timeout_seconds=1)

    async def call_next(_: Request) -> StreamingResponse:
        async def body():
            yield _RESULT_EVENT
            await asyncio.sleep(1.5)
            yield b": trailing keepalive\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    response = await _call(guard, policy, call_next)
    assert await _drain(response) == _RESULT_EVENT


async def test_client_disconnect_releases_the_slot(rate_limit_store) -> None:
    guard, policy = _guard(rate_limit_store, concurrency=1)

    response = await _call(guard, policy, _sse(b": first\n\n", _RESULT_EVENT, delay=0.05))
    iterator = response.body_iterator.__aiter__()
    assert await iterator.__anext__() == b": first\n\n"
    await iterator.aclose()  # client went away mid-stream

    after = await _call(guard, policy, _sse(_RESULT_EVENT), request_id=2)
    assert await _drain(after) == _RESULT_EVENT


async def test_upstream_error_before_headers_releases_the_slot(rate_limit_store) -> None:
    guard, policy = _guard(rate_limit_store, concurrency=1)

    async def boom(_: Request):
        raise RuntimeError("app crashed")

    with pytest.raises(RuntimeError):
        await _call(guard, policy, boom)
    after = await _call(guard, policy, _sse(_RESULT_EVENT), request_id=2)
    assert await _drain(after) == _RESULT_EVENT


def test_end_to_end_slow_tool_over_sse_gets_timeout_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat="test-secret-token",
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
        tool_timeout_seconds=1,
        tool_limit_authenticated_per_min=100,
        expensive_tools="",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    app = create_app()

    async def slow_probe() -> dict:
        await asyncio.sleep(3)
        return {"ok": True}

    app.state.mcp_server.add_tool(slow_probe, name="slow_probe")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-secret-token",
                "Accept": "application/json, text/event-stream",
                "x-workspace-id": "ws-1",
                "x-user-id": "u-1",
            },
            json={
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "slow_probe", "arguments": {}},
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    data_lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert data_lines, response.text
    message = json.loads(data_lines[-1].removeprefix("data: "))
    assert message["id"] == 42
    assert message["result"]["isError"] is True
    assert message["result"]["structuredContent"]["error"]["code"] == "TIMEOUT"
