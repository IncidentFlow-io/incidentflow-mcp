"""
Shared pytest fixtures.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository, TokenRecord
from incidentflow_mcp.auth.tokens import generate_pat
from incidentflow_mcp.config import Settings


# ---------------------------------------------------------------------------
# Settings overrides
# ---------------------------------------------------------------------------


def _settings_with_pat(pat: str | None = "test-secret-token") -> Settings:
    """Return a Settings instance with a known PAT for testing."""
    return Settings(
        incidentflow_pat=pat,
        environment="test",
        log_level="warning",
    )


def _settings_without_pat() -> Settings:
    return Settings(
        incidentflow_pat=None,
        environment="test",
        log_level="warning",
    )


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with a valid legacy PAT configured (plain-text comparison path)."""
    settings = _settings_with_pat("test-secret-token")
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    # Ensure an empty in-memory repo so the repo path never interferes
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def unauth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with NO PAT and NO repo tokens (unprotected mode)."""
    settings = _settings_without_pat()
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def repo_auth_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, str]:
    """
    TestClient backed by an InMemoryTokenRepository with one active token.

    Returns (client, plaintext_token) so tests can use the token in headers.
    INCIDENTFLOW_PAT is intentionally unset — auth must go through the repo.
    """
    settings = _settings_without_pat()
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

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

