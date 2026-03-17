"""
Shared pytest fixtures.
"""

import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository, TokenRecord
from incidentflow_mcp.auth.tokens import generate_pat
from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.redis_store import TokenBucketResult


class InMemoryRateLimitStore:
    """
    Test-only stand-in for RedisRateLimitStore.

    Keeps behavior deterministic for unit tests without external infrastructure.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._concurrency: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    async def take_token(
        self,
        *,
        scope: str,
        identity_key: str,
        limit_per_min: int,
        cost: int = 1,
    ) -> TokenBucketResult:
        key = f"{scope}:{identity_key}"
        now = time.monotonic()
        refill_per_sec = limit_per_min / 60.0

        async with self._lock:
            tokens, ts = self._buckets.get(key, (float(limit_per_min), now))
            elapsed = max(0.0, now - ts)
            tokens = min(float(limit_per_min), tokens + elapsed * refill_per_sec)

            allowed = tokens >= cost
            if allowed:
                tokens -= cost

            self._buckets[key] = (tokens, now)

            deficit = max(0.0, cost - tokens)
            reset_after_ms = int((deficit / refill_per_sec) * 1000) if refill_per_sec > 0 else 0

            return TokenBucketResult(
                allowed=allowed,
                limit=limit_per_min,
                remaining=max(0, int(tokens)),
                reset_after_ms=max(0, reset_after_ms),
            )

    async def acquire_concurrency(
        self,
        *,
        scope: str,
        identity_key: str,
        limit: int,
        ttl_ms: int,
    ) -> bool:
        del ttl_ms
        key = f"{scope}:{identity_key}"
        async with self._lock:
            current = self._concurrency.get(key, 0)
            if current >= limit:
                return False
            self._concurrency[key] = current + 1
            return True

    async def release_concurrency(self, *, scope: str, identity_key: str) -> None:
        key = f"{scope}:{identity_key}"
        async with self._lock:
            current = self._concurrency.get(key, 0)
            if current <= 1:
                self._concurrency.pop(key, None)
                return
            self._concurrency[key] = current - 1


# ---------------------------------------------------------------------------
# Settings overrides
# ---------------------------------------------------------------------------


def _settings_with_pat(pat: str | None = "test-secret-token") -> Settings:
    """Return a Settings instance with a known PAT for testing."""
    return Settings(
        _env_file=None,
        incidentflow_pat=pat,
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
    )


def _settings_without_pat() -> Settings:
    return Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
    )


@pytest.fixture(autouse=True)
def rate_limit_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryRateLimitStore:
    store = InMemoryRateLimitStore()

    def _factory(redis_url: str) -> InMemoryRateLimitStore:
        del redis_url
        return store

    monkeypatch.setattr("incidentflow_mcp.app.RedisRateLimitStore", _factory)
    return store


@pytest.fixture(autouse=True)
def isolate_token_repository(monkeypatch: pytest.MonkeyPatch) -> InMemoryTokenRepository:
    """
    Ensure tests never read developer-local ~/.incidentflow/tokens.json.
    """
    repo = InMemoryTokenRepository()
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)
    return repo


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: InMemoryRateLimitStore,
) -> TestClient:
    """TestClient with a valid legacy PAT configured (plain-text comparison path)."""
    settings = _settings_with_pat("test-secret-token")
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    _ = rate_limit_store
    # Ensure an empty in-memory repo so the repo path never interferes
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def unauth_client(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: InMemoryRateLimitStore,
) -> TestClient:
    """TestClient with NO PAT and NO repo tokens (unprotected mode)."""
    settings = _settings_without_pat()
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    _ = rate_limit_store
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def repo_auth_client(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: InMemoryRateLimitStore,
) -> tuple[TestClient, str]:
    """
    TestClient backed by an InMemoryTokenRepository with one active token.

    Returns (client, plaintext_token) so tests can use the token in headers.
    INCIDENTFLOW_PAT is intentionally unset — auth must go through the repo.
    """
    settings = _settings_without_pat()
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    _ = rate_limit_store

    repo = InMemoryTokenRepository()
    plaintext, token_id, token_hash = generate_pat()
    repo.save(
        TokenRecord(
            token_id=token_id,
            token_hash=token_hash,
            name="test-repo-token",
            scopes=["mcp:read", "mcp:tools:run"],
            created_at=datetime.now(timezone.utc),
        )
    )
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)

    app = create_app()
    return TestClient(app, raise_server_exceptions=False), plaintext


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def valid_auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret-token"}


@pytest.fixture()
def invalid_auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer wrong-token"}
