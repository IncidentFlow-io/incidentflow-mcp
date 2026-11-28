"""MCP permission model: scope required per JSON-RPC method and tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository, TokenRecord
from incidentflow_mcp.auth.scopes import (
    SCOPE_READ,
    SCOPE_TOOLS_RUN,
    SCOPE_TOOLS_WRITE,
    challenge_scope,
    decide,
    effective_scopes,
    requirement_for_payload,
    write_tool_names,
)
from incidentflow_mcp.auth.tokens import generate_pat
from incidentflow_mcp.config import Settings


def _call(tool: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}}


# --- pure model -------------------------------------------------------------


def test_requirement_follows_jsonrpc_method_and_tool() -> None:
    assert requirement_for_payload({"method": "initialize"}).scope == SCOPE_READ
    assert requirement_for_payload({"method": "tools/list"}).scope == SCOPE_READ
    assert requirement_for_payload({"method": "resources/read"}).scope == SCOPE_READ
    assert requirement_for_payload(_call("k8s_list_pods")).scope == SCOPE_TOOLS_RUN
    assert requirement_for_payload(_call("knowledge_upsert")).scope == SCOPE_TOOLS_WRITE
    assert requirement_for_payload(None).scope == SCOPE_READ
    batch = [{"method": "tools/list"}, _call("knowledge_upsert"), _call("mcp_version")]
    assert requirement_for_payload(batch).scope == SCOPE_TOOLS_WRITE


def test_write_tools_come_from_registry_annotations() -> None:
    assert write_tool_names() == {"knowledge_upsert"}


def test_scope_hierarchy() -> None:
    assert effective_scopes([SCOPE_TOOLS_WRITE]) == {SCOPE_TOOLS_WRITE, SCOPE_TOOLS_RUN, SCOPE_READ}
    assert effective_scopes([SCOPE_TOOLS_RUN]) == {SCOPE_TOOLS_RUN, SCOPE_READ}
    assert effective_scopes([SCOPE_READ]) == {SCOPE_READ}


@pytest.mark.parametrize(
    ("granted", "tool", "expected"),
    [
        ([SCOPE_READ], "mcp_version", "deny"),
        ([SCOPE_TOOLS_RUN], "mcp_version", "allow"),
        ([SCOPE_TOOLS_RUN], "knowledge_upsert", "deny"),
        ([SCOPE_TOOLS_WRITE], "knowledge_upsert", "allow"),
        (["incidents:read"], "mcp_version", "deny"),
        ([], "mcp_version", "deny"),
    ],
)
def test_decisions(granted, tool, expected) -> None:
    assert decide(granted, requirement_for_payload(_call(tool))) == expected


def test_unscoped_static_credential_is_allowed() -> None:
    assert decide(None, requirement_for_payload(_call("knowledge_upsert"))) == "allow"


def test_challenge_always_asks_for_read_and_run() -> None:
    assert challenge_scope(requirement_for_payload({"method": "initialize"})) == (
        "mcp:read mcp:tools:run"
    )
    assert challenge_scope(requirement_for_payload(_call("knowledge_upsert"))) == (
        "mcp:read mcp:tools:run mcp:tools:write"
    )


# --- through the app ----------------------------------------------------------


def _client(monkeypatch: pytest.MonkeyPatch, scopes: list[str]) -> tuple[TestClient, str]:
    settings = Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    repo = InMemoryTokenRepository()
    plaintext, token_id, token_hash = generate_pat()
    repo.save(
        TokenRecord(
            token_id=token_id,
            token_hash=token_hash,
            name="perm",
            scopes=scopes,
            created_at=datetime.now(UTC),
        )
    )
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)
    return TestClient(create_app(), raise_server_exceptions=False), plaintext


def _post(client: TestClient, token: str, body: dict[str, Any]):
    return client.post(
        "/mcp",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )


def test_read_only_token_can_discover_but_not_call_tools(monkeypatch) -> None:
    client, token = _client(monkeypatch, [SCOPE_READ])
    with client:
        listed = _post(client, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = _post(client, token, _call("mcp_version"))

    assert listed.status_code == 200
    assert called.status_code == 403
    assert called.json()["error"] == "insufficient_scope"
    assert called.json()["required_scope"] == "mcp:read mcp:tools:run"
    challenge = called.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="mcp:read mcp:tools:run"' in challenge


def test_run_token_calls_read_tools_but_not_write_tools(monkeypatch) -> None:
    client, token = _client(monkeypatch, [SCOPE_TOOLS_RUN])
    with client:
        read = _post(client, token, _call("mcp_version"))
        write = _post(client, token, _call("knowledge_upsert"))

    assert read.status_code == 200
    assert '"isError":false' in read.text.replace(" ", "") or "mcp-version" in read.text
    assert write.status_code == 403
    assert "mcp:tools:write" in write.headers["WWW-Authenticate"]


def test_write_token_reaches_write_tool(monkeypatch) -> None:
    client, token = _client(monkeypatch, [SCOPE_TOOLS_WRITE])
    with client:
        write = _post(client, token, _call("knowledge_upsert"))
    # Authorized: the tool runs (and fails on its own missing arguments, not on auth).
    assert write.status_code == 200


def test_no_header_challenge_advertises_read_and_run(monkeypatch) -> None:
    client, _ = _client(monkeypatch, [SCOPE_READ])
    with client:
        response = client.post("/mcp", json=_call("mcp_version"))
    assert response.status_code == 401
    assert 'scope="mcp:read mcp:tools:run"' in response.headers["WWW-Authenticate"]


def test_introspected_api_key_scopes_and_exchange(monkeypatch) -> None:
    """Workspace/personal API keys: scopes from introspection, Platform gets a JWT."""
    import httpx

    from incidentflow_mcp.auth import middleware

    settings = Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url="https://platform.invalid",
        platform_api_internal_api_key="internal",
        mcp_platform_token_exchange_enabled=True,
        platform_api_token_exchange_api_key="exchange-key",
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            request = httpx.Request("POST", url)
            if url.endswith("/introspect"):
                calls.append("introspect")
                assert kwargs["json"] == {"required_scope": None}
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "active": True,
                        "credential_id": "cred-1",
                        "token_type": "workspace_token",
                        "workspace_id": "ws-1",
                        "user_id": "user-1",
                        "scopes": ["mcp:tools:run"],
                    },
                )
            calls.append("exchange")
            return httpx.Response(
                200, request=request, json={"access_token": "platform-jwt", "expires_in": 600}
            )

    monkeypatch.setattr(middleware.httpx, "AsyncClient", FakeAsyncClient)
    seen: dict[str, Any] = {}
    real_set = middleware._set_auth_context

    def capture(request, **kwargs):
        seen.update(kwargs)
        return real_set(request, **kwargs)

    monkeypatch.setattr(middleware, "_set_auth_context", capture)
    client = TestClient(create_app(), raise_server_exceptions=False)
    with client:
        ok = _post(client, "if_wst_example", _call("mcp_version"))
        denied = _post(client, "if_wst_example", _call("knowledge_upsert"))

    assert ok.status_code == 200
    assert denied.status_code == 403
    assert seen["bearer_token"] == "platform-jwt"  # never the raw key
    assert seen["scopes"] == ["mcp:tools:run"]
    # Second request reused both caches.
    assert calls == ["introspect", "exchange"]


def test_introspection_cache_never_outlives_the_key() -> None:
    from datetime import timedelta

    from incidentflow_mcp.auth import middleware

    middleware.clear_introspection_cache()
    soon = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    middleware._introspection_cache_put("k-soon", None, {"expires_at": soon})
    middleware._introspection_cache_put("k-past", None, {"expires_at": past})
    middleware._introspection_cache_put("k-none", None, {"expires_at": None})

    key = middleware._introspection_cache_key
    soon_deadline = middleware._introspection_cache[key("k-soon", None)][0]
    none_deadline = middleware._introspection_cache[key("k-none", None)][0]
    assert soon_deadline < none_deadline  # capped at ~5 s instead of 30 s
    assert key("k-past", None) not in middleware._introspection_cache
    assert middleware._introspection_cache_get("k-past", None) is None
    middleware.clear_introspection_cache()


def test_production_refuses_the_static_master_token(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat="master",
        platform_api_base_url="https://platform.invalid",
        environment="production",
        log_level="warning",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    with pytest.raises(RuntimeError, match="INCIDENTFLOW_PAT"):
        create_app()


def test_production_refuses_to_start_without_real_auth(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url=None,
        environment="production",
        log_level="warning",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    with pytest.raises(RuntimeError, match="Auth must be configured in production"):
        create_app()
