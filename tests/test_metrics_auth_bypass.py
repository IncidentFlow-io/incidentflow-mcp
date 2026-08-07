"""The unauthenticated /metrics allowlist must not trust a client-supplied
left-most X-Forwarded-For entry (spoof-bypass regression)."""

from __future__ import annotations

import types

import pytest

from incidentflow_mcp.auth import middleware
from incidentflow_mcp.config import Settings


def _req(xff: str | None = None, peer: str | None = "10.0.0.9") -> object:
    headers = {"X-Forwarded-For": xff} if xff else {}
    client = types.SimpleNamespace(host=peer) if peer else None
    return types.SimpleNamespace(headers=headers, client=client)


def test_source_ip_ignores_spoofed_leftmost_xff() -> None:
    # Attacker prepends a trusted IP; the trusted edge appends the real client.
    # The right-most (edge-appended) hop must win, not the spoofed left-most one.
    assert middleware._metrics_source_ip(_req("127.0.0.1, 203.0.113.5")) == "203.0.113.5"
    # No XFF (pod-direct Prometheus scrape) → the socket peer is authoritative.
    assert middleware._metrics_source_ip(_req(None, peer="10.0.0.9")) == "10.0.0.9"


def test_metrics_bypass_denied_for_spoofed_public_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, redis_url="redis://test-only")  # default trusted CIDRs
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)

    # Spoofed trusted IP prepended, real client is public → bypass DENIED.
    assert (
        middleware._is_metrics_request_allowed_without_auth(_req("127.0.0.1, 203.0.113.5")) is False
    )
    # Genuine in-cluster pod-direct scrape (no XFF, peer in 10/8) → allowed.
    assert middleware._is_metrics_request_allowed_without_auth(_req(None, peer="10.0.0.9")) is True
