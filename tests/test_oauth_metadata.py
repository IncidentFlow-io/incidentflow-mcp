from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository
from incidentflow_mcp.config import Settings


def test_protected_resource_metadata_contains_scopes(auth_client: TestClient) -> None:
    response = auth_client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")
    assert "authorization_servers" in body
    assert body["scopes_supported"] == ["mcp:read", "mcp:tools:run"]


def test_protected_resource_metadata_alias(auth_client: TestClient) -> None:
    response = auth_client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")


def test_local_oauth_authorization_server_metadata_bridge(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url="http://localhost:8000",
        oauth_expected_issuer="http://localhost:8000",
        oauth_jwks_url="http://localhost:8000/.well-known/jwks.json",
        environment="test",
        log_level="warning",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())
    client = TestClient(create_app(), raise_server_exceptions=False)

    protected = client.get("/.well-known/oauth-protected-resource")
    assert protected.status_code == 200
    assert protected.json()["authorization_servers"] == ["http://localhost:8000"]

    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    metadata_body = metadata.json()
    assert metadata_body["issuer"] == "http://localhost:8000"
    assert metadata_body["authorization_endpoint"] == "http://localhost:8000/authorize"
    assert metadata_body["token_endpoint"] == "http://localhost:8000/token"
    assert metadata_body["registration_endpoint"] == "http://localhost:8000/register"
    assert metadata_body["jwks_uri"] == "http://localhost:8000/.well-known/jwks.json"
    assert metadata_body["code_challenge_methods_supported"] == ["S256"]
    assert metadata_body["scopes_supported"] == ["mcp:read", "mcp:tools:run"]

    openid = client.get("/.well-known/openid-configuration")
    assert openid.status_code == 200
    openid_body = openid.json()
    assert openid_body["authorization_endpoint"] == "http://localhost:8000/authorize"
    assert "openid" in openid_body["scopes_supported"]
    assert "admin" not in openid_body["scopes_supported"]

    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "client",
            "redirect_uri": "http://localhost/callback",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 307
    assert authorize.headers["location"].startswith("http://localhost:8000/authorize?")
