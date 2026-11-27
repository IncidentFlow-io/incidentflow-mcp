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


def _guard_envelope(body: dict) -> dict:
    result = body["result"]
    assert result["isError"] is True
    envelope = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == envelope
    assert envelope["status"] == "error"
    assert envelope["error"]["details"]["source"] == "mcp_guard"
    return envelope


def _make_client(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> TestClient:
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    return TestClient(create_app(), raise_server_exceptions=False)


class TestTransportRateLimiting:
    def test_unauthenticated_ip_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            _env_file=None,
            incidentflow_pat=None,
            platform_api_base_url=None,
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
            _env_file=None,
            incidentflow_pat="test-secret-token",
            platform_api_base_url=None,
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

    def test_multi_instance_shares_same_store_where_practical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            platform_api_base_url=None,
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            rate_limit_authenticated_per_min=1,
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

        client_one = TestClient(create_app(), raise_server_exceptions=False)
        client_two = TestClient(create_app(), raise_server_exceptions=False)

        headers = {
            "Authorization": "Bearer test-secret-token",
            "x-user-id": "u1",
            "x-workspace-id": "w1",
        }
        req = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}

        first = client_one.post("/mcp", headers=headers, json={**req, "id": 1})
        assert first.status_code != 429

        second = client_two.post("/mcp", headers=headers, json={**req, "id": 2})
        assert second.status_code == 429


class TestToolRateLimiting:
    def test_expensive_tool_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            platform_api_base_url=None,
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            tool_limit_authenticated_per_min=10,
            expensive_tool_limit_per_min=1,
            expensive_tools="incident_summary",
        )
        client = _make_client(monkeypatch, settings)

        headers = {
            "Authorization": "Bearer test-secret-token",
            "x-workspace-id": "ws-1",
            "x-user-id": "u-1",
        }

        first = client.post("/mcp", headers=headers, json=MCP_TOOL_REQUEST)
        assert first.status_code != 429

        second = client.post("/mcp", headers=headers, json={**MCP_TOOL_REQUEST, "id": 2})
        assert second.status_code == 200
        assert int(second.headers["Retry-After"]) >= 1
        body = second.json()
        assert body["id"] == 2 and "error" not in body
        envelope = _guard_envelope(body)
        assert envelope["schema_id"] == "incidentflow.incident-summary.response"
        assert envelope["error"]["code"] == "RATE_LIMITED"
        assert envelope["error"]["retryable"] is True
        assert envelope["error"]["message"] == "Rate limit exceeded for tool invocation"
        assert envelope["error"]["details"]["limit_scope"] == "tool_expensive"
        assert envelope["error"]["details"]["retry_after_seconds"] >= 1

    def test_transport_limit_on_tools_call_returns_rate_limited_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            platform_api_base_url=None,
            environment="test",
            log_level="warning",
            redis_url="redis://test-only",
            rate_limit_authenticated_per_min=1,
            tool_limit_authenticated_per_min=100,
            expensive_tools="",
        )
        client = _make_client(monkeypatch, settings)
        headers = {
            "Authorization": "Bearer test-secret-token",
            "x-workspace-id": "ws-1",
            "x-user-id": "u-1",
        }

        client.post("/mcp", headers=headers, json=MCP_TOOL_REQUEST)
        limited = client.post("/mcp", headers=headers, json={**MCP_TOOL_REQUEST, "id": 7})

        assert limited.status_code == 200
        assert limited.headers["Retry-After"]
        assert limited.headers["X-RateLimit-Limit"] == "1"
        envelope = _guard_envelope(limited.json())
        assert limited.json()["id"] == 7
        assert envelope["error"]["code"] == "RATE_LIMITED"
        assert envelope["error"]["retryable"] is True
        assert envelope["error"]["details"]["limit_scope"] == "transport"
        assert envelope["error"]["details"]["retry_after_seconds"] == int(
            limited.headers["Retry-After"]
        )


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
    async def test_concurrency_limit_enforced(self, rate_limit_store) -> None:
        settings = Settings(
            _env_file=None,
            redis_url="redis://test-only",
            platform_api_base_url=None,
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

        envelope = _guard_envelope(json.loads(second.body.decode("utf-8")))
        assert envelope["error"]["code"] == "RATE_LIMITED"
        assert envelope["error"]["retryable"] is True
        assert envelope["error"]["message"] == "Too many concurrent tool invocations"
        assert envelope["error"]["details"]["reason"] == "concurrency_limit"
        assert second.headers["Retry-After"] == "1"

    @pytest.mark.asyncio
    async def test_timeout_behavior(self, rate_limit_store) -> None:
        settings = Settings(
            _env_file=None,
            redis_url="redis://test-only",
            platform_api_base_url=None,
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

        envelope = _guard_envelope(json.loads(result.body.decode("utf-8")))
        assert envelope["error"]["code"] == "TIMEOUT"
        assert envelope["error"]["retryable"] is True
        assert envelope["error"]["message"] == "Tool execution timed out"


def test_guard_envelope_is_classified_as_rejection_not_tool_failure() -> None:
    from incidentflow_mcp.observability.middleware import _mcp_tool_event_from_body
    from incidentflow_mcp.rate_limit.tool_guard import tool_guard_error_response
    from incidentflow_mcp.tools.contracts import ErrorCode

    response = tool_guard_error_response(
        tool_call=MCPToolCall(request_id=1, tool_name="logs_query"),
        code=ErrorCode.RATE_LIMITED,
        reason="rate_limited",
        limit_scope="tool",
        message="Rate limit exceeded for tool invocation",
        retry_after_seconds=3,
    )
    event = _mcp_tool_event_from_body(
        body=response.body, content_type="application/json", tool_name="logs_query"
    )
    assert event is not None
    assert event["outcome"] == "rejected"
    assert event["reason"] == "rate_limited"
    assert event["error_code"] == "RATE_LIMITED"
    assert event["retryable"] is True


def test_upstream_retry_after_is_preserved_in_error_details() -> None:
    import httpx

    from incidentflow_mcp.mcp.errors import map_exception
    from incidentflow_mcp.platform_api.logs_client import PlatformLogsError

    request = httpx.Request("GET", "https://platform.invalid/x")
    response = httpx.Response(429, headers={"Retry-After": "7"}, request=request)
    fields = map_exception(httpx.HTTPStatusError("429", request=request, response=response))
    assert fields["code"] == "RATE_LIMITED"
    assert fields["retryable"] is True
    assert fields["details"]["retry_after_seconds"] == 7

    exc = PlatformLogsError(429, 5)
    assert exc.code == "RATE_LIMITED" and exc.retry_after_seconds == 5


@pytest.mark.asyncio
async def test_logs_client_reads_upstream_retry_after() -> None:
    import httpx

    from incidentflow_mcp.platform_api.logs_client import PlatformLogsClient, PlatformLogsError

    settings = Settings(
        _env_file=None,
        platform_api_base_url="https://platform.invalid",
        platform_api_internal_api_key="internal-key",
        redis_url="redis://test-only",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "4"})
    )
    client = PlatformLogsClient(settings, "bearer", transport=transport)
    with pytest.raises(PlatformLogsError) as caught:
        await client.query({"source_id": "f12f7829-0ed4-463c-8bd6-9f6f459bcc28"})
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after_seconds == 4


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [(429, "RATE_LIMITED", True), (502, "UPSTREAM_ERROR", True), (400, "INVALID_ARGUMENT", False)],
)
def test_legacy_integration_error_label_keeps_http_derived_code(
    status: int, expected_code: str, retryable: bool
) -> None:
    import httpx

    from incidentflow_mcp.mcp.errors import TOOL_ERROR_MARKER, structured_tool_exception

    request = httpx.Request("POST", "https://platform.invalid/grafana/query")
    response = httpx.Response(status, request=request, json={"detail": "x"})
    exc = httpx.HTTPStatusError("upstream", request=request, response=response)
    for label in ("GRAFANA_HTTP_ERROR", "ARGOCD_HTTP_ERROR"):
        fields = structured_tool_exception(exc, code=label)[TOOL_ERROR_MARKER]
        assert fields["code"] == expected_code
        assert fields["retryable"] is retryable
        assert fields["details"]["http_status"] == status
        assert fields["details"]["integration_error"] == label
