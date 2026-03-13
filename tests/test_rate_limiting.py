"""Rate limiting and tool guard tests."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository
from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.identity import ResolvedIdentity
from incidentflow_mcp.rate_limit.policy import DefaultPolicyResolver
from incidentflow_mcp.rate_limit.tool_guard import MCPToolCall, ToolInvocationGuard


MCP_TOOL_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "incident_summary",
        "arguments": {
            "incident_id": "INC-001",
            "include_timeline": False,
            "include_affected_services": False,
        },
    },
}


def _make_client(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> TestClient:
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    return TestClient(create_app(), raise_server_exceptions=False)


class TestTransportRateLimiting:
    def test_unauthenticated_ip_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            incidentflow_pat=None,
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            rate_limit_unauth_per_min=2,
            rate_limit_auth_endpoints="/authorize",
        )
        client = _make_client(monkeypatch, settings)

        assert client.post("/authorize").status_code in (404, 405)
        assert client.post("/authorize").status_code in (404, 405)

        limited = client.post("/authorize")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"]
        assert limited.headers["X-RateLimit-Limit"] == "2"
        assert "X-RateLimit-Remaining" in limited.headers
        assert "X-RateLimit-Reset" in limited.headers

    def test_authenticated_principal_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            incidentflow_pat="test-secret-token",
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            rate_limit_authenticated_per_min=2,
        )
        client = _make_client(monkeypatch, settings)

        headers = {
            "Authorization": "Bearer test-secret-token",
            "x-workspace-id": "ws-1",
            "x-user-id": "u-1",
            "x-plan-tier": "pro",  # preserved as metadata only; no semantic mapping in core
        }

        req = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}
        assert client.post("/mcp", headers=headers, json={**req, "id": 1}).status_code != 429
        assert client.post("/mcp", headers=headers, json={**req, "id": 2}).status_code != 429
        assert client.post("/mcp", headers=headers, json={**req, "id": 3}).status_code == 429

    def test_multi_instance_shares_same_store_where_practical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            incidentflow_pat="test-secret-token",
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            rate_limit_authenticated_per_min=1,
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

        client_one = TestClient(create_app(), raise_server_exceptions=False)
        client_two = TestClient(create_app(), raise_server_exceptions=False)

        headers = {"Authorization": "Bearer test-secret-token", "x-user-id": "u1", "x-workspace-id": "w1"}
        req = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}

        first = client_one.post("/mcp", headers=headers, json={**req, "id": 1})
        assert first.status_code != 429

        second = client_two.post("/mcp", headers=headers, json={**req, "id": 2})
        assert second.status_code == 429


class TestToolRateLimiting:
    def test_expensive_tool_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            incidentflow_pat="test-secret-token",
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            tool_limit_authenticated_per_min=10,
            expensive_tool_limit_per_min=1,
            expensive_tools="incident_summary",
        )
        client = _make_client(monkeypatch, settings)

        headers = {"Authorization": "Bearer test-secret-token", "x-workspace-id": "ws-1", "x-user-id": "u-1"}

        first = client.post("/mcp", headers=headers, json=MCP_TOOL_REQUEST)
        assert first.status_code != 429

        second = client.post("/mcp", headers=headers, json={**MCP_TOOL_REQUEST, "id": 2})
        assert second.status_code == 200
        body = second.json()
        assert body["error"]["message"] == "Rate limit exceeded for tool invocation"


class TestToolGuardUnit:
    def _request(self) -> Request:
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

    @pytest.mark.asyncio
    async def test_concurrency_limit_enforced(self, rate_limit_store) -> None:  # noqa: ANN001
        settings = Settings(
            redis_url="redis://test-only",
            tool_concurrency_authenticated=1,
            tool_limit_authenticated_per_min=100,
            expensive_tools="",
        )
        resolver = DefaultPolicyResolver(settings)
        guard = ToolInvocationGuard(rate_limit_store, resolver, BucketKeyResolver())

        identity = ResolvedIdentity(
            authenticated=True,
            ip_address="127.0.0.1",
            workspace_id="w1",
            user_id="u1",
            client_id="c1",
            plan="whatever",
        )
        policy = resolver.resolve(identity)
        tool_call = MCPToolCall(request_id=1, tool_name="incident_summary")
        request = self._request()

        async def slow_call_next(_: Request) -> JSONResponse:
            await asyncio.sleep(0.2)
            return JSONResponse(status_code=200, content={"ok": True})

        async def fast_call_next(_: Request) -> JSONResponse:
            return JSONResponse(status_code=200, content={"ok": True})

        first_task = asyncio.create_task(
            guard.guard(
                request=request,
                call_next=slow_call_next,
                identity=identity,
                policy=policy,
                tool_call=tool_call,
            )
        )
        await asyncio.sleep(0.01)

        second = await guard.guard(
            request=request,
            call_next=fast_call_next,
            identity=identity,
            policy=policy,
            tool_call=MCPToolCall(request_id=2, tool_name="incident_summary"),
        )
        await first_task

        body = json.loads(second.body.decode("utf-8"))
        assert body["error"]["message"] == "Too many concurrent tool invocations"

    @pytest.mark.asyncio
    async def test_timeout_behavior(self, rate_limit_store) -> None:  # noqa: ANN001
        settings = Settings(
            redis_url="redis://test-only",
            tool_timeout_seconds=1,
            tool_limit_authenticated_per_min=100,
            expensive_tools="",
        )
        resolver = DefaultPolicyResolver(settings)
        guard = ToolInvocationGuard(rate_limit_store, resolver, BucketKeyResolver())

        identity = ResolvedIdentity(
            authenticated=True,
            ip_address="127.0.0.1",
            workspace_id="w1",
            user_id="u1",
            client_id="c1",
            plan="example-plan",
        )
        policy = resolver.resolve(identity)
        request = self._request()

        async def very_slow(_: Request) -> JSONResponse:
            await asyncio.sleep(2)
            return JSONResponse(status_code=200, content={"ok": True})

        result = await guard.guard(
            request=request,
            call_next=very_slow,
            identity=identity,
            policy=policy,
            tool_call=MCPToolCall(request_id=9, tool_name="incident_summary"),
        )

        body = json.loads(result.body.decode("utf-8"))
        assert body["error"]["message"] == "Tool execution timed out"
