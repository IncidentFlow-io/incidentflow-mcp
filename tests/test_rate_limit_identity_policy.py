"""Tests for identity, policy and bucket-key separation."""

from __future__ import annotations

from fastapi import Request

from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.identity import IdentityResolver, ResolvedIdentity
from incidentflow_mcp.rate_limit.policy import DefaultPolicyResolver, RateLimitPolicy


def _request(*, headers: list[tuple[bytes, bytes]], auth_context: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": headers,
        "query_string": b"",
        "client": ("10.0.0.9", 1234),
        "server": ("test", 80),
        "scheme": "http",
        "http_version": "1.1",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(scope, receive)
    if auth_context is not None:
        req.state.auth_context = auth_context
    return req


def test_identity_resolution_prefers_auth_context_then_headers() -> None:
    req = _request(
        headers=[
            (b"x-workspace-id", b"ws-h"),
            (b"x-user-id", b"u-h"),
            (b"x-client-id", b"c-h"),
            (b"x-plan", b" plan-from-header "),
        ],
        auth_context={
            "authenticated": True,
            "workspace_id": "ws-auth",
            "user_id": "u-auth",
            "client_id": "c-auth",
            "plan": " plan-from-auth ",
        },
    )

    identity = IdentityResolver().resolve(req)

    assert identity.authenticated is True
    assert identity.workspace_id == "ws-auth"
    assert identity.user_id == "u-auth"
    assert identity.client_id == "c-auth"
    assert identity.plan == "plan-from-auth"


def test_plan_metadata_is_raw_not_remapped() -> None:
    req = _request(
        headers=[(b"x-plan-tier", b" Pro ")],
        auth_context={"authenticated": True},
    )

    identity = IdentityResolver().resolve(req)
    assert identity.plan == "Pro"


def test_principal_key_resolution_order() -> None:
    identity_ws_user = ResolvedIdentity(True, "1.1.1.1", "ws", "u", "c", None)
    identity_client = ResolvedIdentity(True, "1.1.1.1", None, None, "c", None)
    identity_ip = ResolvedIdentity(True, "1.1.1.1", None, None, None, None)

    assert identity_ws_user.principal_key == "workspace:ws:user:u"
    assert identity_client.principal_key == "client:c"
    assert identity_ip.principal_key == "ip:1.1.1.1"


def test_bucket_key_resolution_depends_on_bucket_scope() -> None:
    identity = ResolvedIdentity(
        authenticated=True,
        ip_address="10.0.0.9",
        workspace_id="ws1",
        user_id="u1",
        client_id="c1",
        plan="whatever",
    )
    resolver = BucketKeyResolver()

    workspace_policy = RateLimitPolicy(
        name="p1",
        bucket_scope="workspace",
        transport_limit_per_min=1,
        tool_limit_per_min=1,
        expensive_tool_limit_per_min=1,
        concurrency_limit=1,
        timeout_seconds=1,
    )
    principal_policy = RateLimitPolicy(
        name="p2",
        bucket_scope="principal",
        transport_limit_per_min=1,
        tool_limit_per_min=1,
        expensive_tool_limit_per_min=1,
        concurrency_limit=1,
        timeout_seconds=1,
    )
    ip_policy = RateLimitPolicy(
        name="p3",
        bucket_scope="ip",
        transport_limit_per_min=1,
        tool_limit_per_min=1,
        expensive_tool_limit_per_min=1,
        concurrency_limit=1,
        timeout_seconds=1,
    )

    assert resolver.transport_key(identity, workspace_policy) == "workspace:ws1"
    assert resolver.transport_key(identity, principal_policy) == "workspace:ws1:user:u1"
    assert resolver.transport_key(identity, ip_policy) == "ip:10.0.0.9"


def test_default_policy_resolver_is_authenticated_vs_unauthenticated_only() -> None:
    settings = Settings(
        _env_file=None,
        redis_url="redis://test-only",
        platform_api_base_url=None,
        rate_limit_unauth_per_min=7,
        rate_limit_authenticated_per_min=42,
        tool_limit_authenticated_per_min=11,
    )
    resolver = DefaultPolicyResolver(settings)

    unauth = ResolvedIdentity(False, "1.1.1.1", None, None, None, "random-plan-a")
    auth = ResolvedIdentity(True, "1.1.1.1", None, None, None, "random-plan-b")

    p1 = resolver.resolve(unauth)
    p2 = resolver.resolve(auth)

    assert p1.name == "default_unauthenticated"
    assert p1.transport_limit_per_min == 7
    assert p2.name == "default_authenticated"
    assert p2.transport_limit_per_min == 42
    assert p2.tool_limit_per_min == 11
