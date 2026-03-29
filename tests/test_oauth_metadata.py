from fastapi.testclient import TestClient


def test_protected_resource_metadata_contains_scopes(auth_client: TestClient) -> None:
    response = auth_client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")
    assert "authorization_servers" in body
    assert body["scopes_supported"] == ["mcp:read", "mcp:tools:run", "admin"]


def test_protected_resource_metadata_alias(auth_client: TestClient) -> None:
    response = auth_client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")
