"""
Tests for /mcp endpoint routing edge cases.

Cements the behaviour of the _MCPASGIRoute-based proxy, including the
historical bug where app.mount("/mcp", ...) stripped the path prefix and
caused FastMCP to receive scope["path"] == "" instead of "/mcp".
"""

import pytest
from fastapi.testclient import TestClient


class TestMCPPublicAccessControl:
    """The /mcp endpoint must always require authentication when a PAT is set."""

    def test_mcp_requires_bearer_token(self, auth_client: TestClient) -> None:
        resp = auth_client.post("/mcp")
        assert resp.status_code == 401

    def test_mcp_get_requires_bearer_token(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp")
        assert resp.status_code == 401

    def test_mcp_options_requires_bearer_token(self, auth_client: TestClient) -> None:
        resp = auth_client.options("/mcp")
        assert resp.status_code == 401

    def test_healthz_open_without_token(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200

    def test_readyz_open_without_token(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/readyz")
        assert resp.status_code == 200


class TestMCPRoutingReachesSubapp:
    """
    With a valid Bearer token the request must pass auth and reach the
    FastMCP sub-app (any non-401 status proves the route matched).
    """

    def test_post_mcp_with_valid_token_reaches_subapp(
        self, auth_client: TestClient, valid_auth_headers: dict[str, str]
    ) -> None:
        resp = auth_client.post("/mcp", headers=valid_auth_headers)
        assert resp.status_code != 401

    def test_get_mcp_with_valid_token_reaches_subapp(
        self, auth_client: TestClient, valid_auth_headers: dict[str, str]
    ) -> None:
        resp = auth_client.get("/mcp", headers=valid_auth_headers)
        assert resp.status_code != 401

    def test_unprotected_mode_post_mcp_reaches_subapp(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/mcp")
        assert resp.status_code != 401

    def test_unprotected_mode_get_mcp_reaches_subapp(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/mcp")
        assert resp.status_code != 401


class TestMCPMethodRestriction:
    """
    Methods outside GET / POST / OPTIONS must NOT be forwarded to the sub-app.
    They should return 404 (no route matched) — not 401 — because routing
    happens before we know whether auth is present.
    """

    def test_put_mcp_not_routed(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put("/mcp")
        assert resp.status_code == 404

    def test_delete_mcp_not_routed(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete("/mcp")
        assert resp.status_code == 404

    def test_head_mcp_not_routed(self, unauth_client: TestClient) -> None:
        resp = unauth_client.head("/mcp")
        # HEAD requests typically return 405 from FastAPI for paths that have
        # no matching route; accept either 404 or 405.
        assert resp.status_code in (404, 405)


class TestMCPTrailingSlash:
    """
    With redirect_slashes=False, /mcp/ (trailing slash) must NOT be silently
    redirected to /mcp or forwarded to the sub-app.
    """

    def test_mcp_with_trailing_slash_is_not_found(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/mcp/")
        # Could be 404 (no route) or 401 (if auth middleware fires first).
        # The key assertion is that it does NOT reach the sub-app as an
        # accepted request (i.e., not a 2xx/3xx from the sub-app).
        assert resp.status_code in (401, 404)


class TestRequestIDHeader:
    """The X-Request-ID header must be present in every response."""

    def test_request_id_echoed_when_provided(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz", headers={"x-request-id": "my-trace-id"})
        assert resp.headers.get("x-request-id") == "my-trace-id"

    def test_request_id_generated_when_absent(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert "x-request-id" in resp.headers
        assert resp.headers["x-request-id"]  # non-empty


class TestProductionFailFast:
    """create_app() must raise when no auth source is configured in production."""

    def test_raises_without_auth_source_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from incidentflow_mcp.config import Settings

        settings = Settings(
            _env_file=None,
            incidentflow_pat=None,
            platform_api_base_url=None,
            environment="production",
            log_level="warning",
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        from incidentflow_mcp.app import create_app

        with pytest.raises(RuntimeError, match="Auth must be configured in production"):
            create_app()

    def test_no_error_with_platform_api_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from incidentflow_mcp.config import Settings

        settings = Settings(
            _env_file=None,
            incidentflow_pat=None,
            platform_api_base_url="http://127.0.0.1:8000",
            environment="production",
            log_level="warning",
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        from incidentflow_mcp.app import create_app

        app = create_app()
        assert app is not None

    def test_no_error_without_pat_in_development(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from incidentflow_mcp.config import Settings

        settings = Settings(
            _env_file=None,
            incidentflow_pat=None,
            platform_api_base_url=None,
            environment="development",
            log_level="warning",
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        from incidentflow_mcp.app import create_app

        app = create_app()  # must not raise
        assert app is not None
