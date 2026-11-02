import pytest
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


def test_explicit_canonical_resource_persists_without_jwt_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://mcp-dev.incidentflow.io"
    settings = Settings(
        _env_file=None,
        environment="test",
        mcp_canonical_resource=f"{issuer}/mcp",
        mcp_resource_metadata_url=f"{issuer}/.well-known/oauth-protected-resource",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get(
        "/.well-known/oauth-protected-resource",
        headers={"X-Forwarded-Proto": "http", "X-Forwarded-Host": "attacker.example"},
    )
    assert response.status_code == 200
    assert response.json()["resource"] == f"{issuer}/mcp"
    assert response.json()["authorization_servers"] == [issuer]


def test_invalid_explicit_canonical_resource_without_jwt_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        mcp_canonical_resource="http://mcp-dev.incidentflow.io/mcp",
        mcp_resource_metadata_url=(
            "http://mcp-dev.incidentflow.io/.well-known/oauth-protected-resource"
        ),
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    with pytest.raises(ValueError, match="OAUTH_EXPECTED_ISSUER"):
        create_app()


def test_local_oauth_authorization_server_metadata_bridge(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_pat=None,
        platform_api_base_url="http://localhost:8000",
        oauth_expected_issuer="http://localhost:8000",
        oauth_jwks_url="http://localhost:8000/.well-known/jwks.json",
        mcp_canonical_resource="http://localhost:8000/mcp",
        mcp_resource_metadata_url="http://localhost:8000/.well-known/oauth-protected-resource",
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
    assert protected.json()["resource"] == "http://localhost:8000/mcp"

    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    metadata_body = metadata.json()
    assert metadata_body["issuer"] == "http://localhost:8000"
    assert metadata_body["authorization_endpoint"] == "http://localhost:8000/authorize"
    assert metadata_body["token_endpoint"] == "http://localhost:8000/token"
    assert metadata_body["registration_endpoint"] == "http://localhost:8000/register"
    assert metadata_body["jwks_uri"] == "http://localhost:8000/.well-known/jwks.json"
    assert metadata_body["authorization_response_iss_parameter_supported"] is True
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


@pytest.mark.parametrize(
    ("issuer", "resource"),
    [
        ("https://mcp-dev.incidentflow.io", "https://mcp-dev.incidentflow.io/mcp"),
        ("https://mcp.incidentflow.io", "https://mcp.incidentflow.io/mcp"),
    ],
)
def test_public_discovery_ignores_proxy_headers(
    monkeypatch: pytest.MonkeyPatch, issuer: str, resource: str
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        incidentflow_pat=None,
        platform_api_base_url="http://platform-api.internal",
        oauth_expected_issuer=issuer,
        oauth_jwks_url=f"{issuer}/.well-known/jwks.json",
        mcp_canonical_resource=resource,
        mcp_resource_metadata_url=f"{issuer}/.well-known/oauth-protected-resource",
        redis_url="redis://test-only",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    client = TestClient(create_app(), raise_server_exceptions=False)
    headers = {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "attacker.example"}

    base = client.get("/.well-known/oauth-protected-resource", headers=headers)
    alias = client.get("/.well-known/oauth-protected-resource/mcp", headers=headers)
    assert base.status_code == alias.status_code == 200
    assert (
        base.json()
        == alias.json()
        == {
            "resource": resource,
            "authorization_servers": [issuer],
            "scopes_supported": ["mcp:read", "mcp:tools:run"],
        }
    )
    as_metadata = client.get("/.well-known/oauth-authorization-server", headers=headers).json()
    oidc = client.get("/.well-known/openid-configuration", headers=headers).json()
    assert as_metadata["issuer"] == oidc["issuer"] == issuer
    assert as_metadata["authorization_endpoint"] == f"{issuer}/authorize"
    assert as_metadata["token_endpoint"] == f"{issuer}/token"
    assert as_metadata["registration_endpoint"] == f"{issuer}/register"
    assert as_metadata["authorization_response_iss_parameter_supported"] is True


@pytest.mark.parametrize(
    ("issuer", "resource", "metadata"),
    [
        (
            "http://mcp-dev.incidentflow.io",
            "http://mcp-dev.incidentflow.io/mcp",
            "http://mcp-dev.incidentflow.io/.well-known/oauth-protected-resource",
        ),
        (
            "https://mcp-dev.incidentflow.io",
            "http://mcp-dev.incidentflow.io/mcp",
            "https://mcp-dev.incidentflow.io/.well-known/oauth-protected-resource",
        ),
        (
            "https://mcp-dev.incidentflow.io",
            "https://mcp-dev.incidentflow.io/mcp",
            "http://mcp-dev.incidentflow.io/.well-known/oauth-protected-resource",
        ),
    ],
)
def test_invalid_public_oauth_identity_fails_startup(
    monkeypatch: pytest.MonkeyPatch, issuer: str, resource: str, metadata: str
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        oauth_expected_issuer=issuer,
        oauth_jwks_url="https://mcp-dev.incidentflow.io/.well-known/jwks.json",
        mcp_canonical_resource=resource,
        mcp_resource_metadata_url=metadata,
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    with pytest.raises(
        ValueError,
        match=r"OAUTH_EXPECTED_ISSUER|MCP_CANONICAL_RESOURCE|MCP_RESOURCE_METADATA_URL",
    ):
        create_app()


def test_partial_oauth_configuration_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, oauth_expected_issuer="https://mcp-dev.incidentflow.io")
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    with pytest.raises(ValueError, match="OAUTH_EXPECTED_ISSUER and OAUTH_JWKS_URL"):
        create_app()
