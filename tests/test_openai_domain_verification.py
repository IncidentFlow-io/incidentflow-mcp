import pytest
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository
from incidentflow_mcp.config import Settings


def test_openai_domain_verification_is_not_public_when_not_configured(
    auth_client: TestClient,
) -> None:
    response = auth_client.get("/.well-known/openai-domain-verification")

    assert response.status_code == 401


def test_openai_domain_verification_is_public_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat="test-secret-token",
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
        openai_domain_verification_path="/.well-known/openai-domain-verification",
        openai_domain_verification_token="verify-token-123",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get("/.well-known/openai-domain-verification")

    assert response.status_code == 200
    assert response.text == "verify-token-123"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"


def test_openai_domain_verification_does_not_shadow_oauth_metadata(
    auth_client: TestClient,
) -> None:
    response = auth_client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["scopes_supported"] == ["mcp:read", "mcp:tools:run"]
