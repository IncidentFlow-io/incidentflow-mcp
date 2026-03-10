"""
Tests for the /healthz endpoint.
"""

from fastapi.testclient import TestClient


class TestHealthz:
    def test_returns_200(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200

    def test_response_body_structure(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        body = resp.json()
        assert body["status"] == "ok"
        assert "service" in body
        assert "version" in body

    def test_content_type_json(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert "application/json" in resp.headers["content-type"]

    def test_no_auth_required(self, auth_client: TestClient) -> None:
        """Health endpoint must not require a Bearer token."""
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200
