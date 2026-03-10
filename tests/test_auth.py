"""
Tests for the Bearer PAT authentication middleware.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from incidentflow_mcp.app import create_app
from incidentflow_mcp.auth.repository import InMemoryTokenRepository, TokenRecord
from incidentflow_mcp.auth.tokens import generate_pat
from incidentflow_mcp.config import Settings


class TestHealthzNoAuth:
    """The health endpoint is always public."""

    def test_healthz_no_header(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_unprotected_mode(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/healthz")
        assert resp.status_code == 200


class TestAuthMissing:
    def test_missing_auth_header_returns_401(self, auth_client: TestClient) -> None:
        # The /mcp path is protected; a request without auth should be 401
        resp = auth_client.get("/mcp")
        assert resp.status_code == 401

    def test_www_authenticate_header_present(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp")
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    def test_response_body_has_detail(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp")
        body = resp.json()
        assert "detail" in body


class TestAuthMalformed:
    def test_basic_scheme_returns_401(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_empty_bearer_returns_401(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_token_via_query_param_returns_401(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp?token=test-secret-token")
        assert resp.status_code == 401

    def test_access_token_query_param_returns_401(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/mcp?access_token=test-secret-token")
        assert resp.status_code == 401


class TestAuthInvalid:
    def test_wrong_token_returns_401(
        self, auth_client: TestClient, invalid_auth_headers: dict[str, str]
    ) -> None:
        resp = auth_client.get("/mcp", headers=invalid_auth_headers)
        assert resp.status_code == 401

    def test_token_case_insensitive_scheme(self, auth_client: TestClient) -> None:
        # Scheme matching is case-insensitive per RFC 7235.
        # Auth passes, so the response must NOT be 401.
        # The MCP sub-app may return any non-401 status (404/405/500)
        # because GET is not a valid MCP transport method.
        resp = auth_client.get(
            "/mcp", headers={"Authorization": "BEARER test-secret-token"}
        )
        assert resp.status_code != 401


class TestAuthSuccess:
    def test_valid_token_passes_middleware(
        self, auth_client: TestClient, valid_auth_headers: dict[str, str]
    ) -> None:
        # /mcp with valid token: middleware passes the request through.
        # The MCP sub-app will handle it — we just verify it's not 401.
        resp = auth_client.get("/mcp", headers=valid_auth_headers)
        assert resp.status_code != 401

    def test_unprotected_mode_no_token_needed(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/mcp")
        assert resp.status_code != 401


class TestRepoAuth:
    """Tests for structured PAT verification via the token repository."""

    def test_repo_valid_token_passes(
        self, repo_auth_client: tuple[TestClient, str]
    ) -> None:
        client, token = repo_auth_client
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code != 401

    def test_repo_wrong_secret_returns_401(
        self, repo_auth_client: tuple[TestClient, str]
    ) -> None:
        client, token = repo_auth_client
        # keep the token_id but corrupt the secret portion
        token_id = token.split(".")[0]  # "if_pat_local_<id>"
        tampered = f"{token_id}.wrongsecretXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {tampered}"})
        assert resp.status_code == 401

    def test_repo_unknown_token_id_returns_401(
        self, repo_auth_client: tuple[TestClient, str]
    ) -> None:
        client, _ = repo_auth_client
        unknown = "if_pat_local_deadbeef.Nx8K3mQp7Wz2Rk9Ls1Vf0Yt6AbCdEfGhIjKlMnOp"
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {unknown}"})
        assert resp.status_code == 401

    def test_repo_revoked_token_returns_401(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = Settings(incidentflow_pat=None, environment="test", log_level="warning")
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        repo = InMemoryTokenRepository()
        plaintext, token_id, token_hash = generate_pat()
        now = datetime.now(timezone.utc)
        repo.save(
            TokenRecord(
                token_id=token_id,
                token_hash=token_hash,
                name="revoked-token",
                scopes=["mcp:read"],
                created_at=now,
                revoked_at=now,  # already revoked
            )
        )
        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)

        client = TestClient(create_app(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {plaintext}"})
        assert resp.status_code == 401
        assert "revoked" in resp.json()["detail"].lower()

    def test_repo_expired_token_returns_401(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = Settings(incidentflow_pat=None, environment="test", log_level="warning")
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        repo = InMemoryTokenRepository()
        plaintext, token_id, token_hash = generate_pat()
        now = datetime.now(timezone.utc)
        repo.save(
            TokenRecord(
                token_id=token_id,
                token_hash=token_hash,
                name="expired-token",
                scopes=["mcp:read"],
                created_at=now - timedelta(days=10),
                expires_at=now - timedelta(seconds=1),  # expired 1 second ago
            )
        )
        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)

        client = TestClient(create_app(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {plaintext}"})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_repo_no_auth_header_returns_401(
        self, repo_auth_client: tuple[TestClient, str]
    ) -> None:
        # When a repo token exists, the server is protected — no auth → 401
        client, _ = repo_auth_client
        resp = client.get("/mcp")
        assert resp.status_code == 401


class TestScopeEnforcement:
    """Scope checks on structured repo tokens."""

    def _make_client_with_scopes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scopes: list[str],
        enforce: bool,
    ) -> tuple[TestClient, str]:
        settings = Settings(
            incidentflow_pat=None,
            environment="production" if enforce else "development",
            log_level="warning",
        )
        monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

        repo = InMemoryTokenRepository()
        plaintext, token_id, token_hash = generate_pat()
        repo.save(
            TokenRecord(
                token_id=token_id,
                token_hash=token_hash,
                name="scope-test-token",
                scopes=scopes,
                created_at=datetime.now(timezone.utc),
            )
        )
        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", repo)
        client = TestClient(create_app(), raise_server_exceptions=False)
        return client, plaintext

    # --- enforcement enabled (production) ---

    def test_missing_scope_returns_403_when_enforced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Token has only mcp:read; /mcp requires mcp:read → should pass
        # But a token with NO scopes at all should get 403
        client, token = self._make_client_with_scopes(monkeypatch, scopes=[], enforce=True)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "insufficient_scope"
        assert "required_scope" in body

    def test_403_body_contains_required_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, token = self._make_client_with_scopes(monkeypatch, scopes=[], enforce=True)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert resp.json()["required_scope"] == "mcp:read"

    def test_sufficient_scope_passes_enforcement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, token = self._make_client_with_scopes(
            monkeypatch, scopes=["mcp:read", "mcp:tools:run"], enforce=True
        )
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code != 401
        assert resp.status_code != 403

    def test_tools_run_scope_required_for_mcp_tools_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Token has mcp:read only — /mcp/tools requires mcp:tools:run
        client, token = self._make_client_with_scopes(
            monkeypatch, scopes=["mcp:read"], enforce=True
        )
        resp = client.post("/mcp/tools", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert resp.json()["required_scope"] == "mcp:tools:run"

    def test_admin_scope_required_for_admin_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, token = self._make_client_with_scopes(
            monkeypatch, scopes=["mcp:read", "mcp:tools:run"], enforce=True
        )
        resp = client.get("/admin/tokens", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert resp.json()["required_scope"] == "admin"

    # --- enforcement disabled (dev mode) ---

    def test_missing_scope_passes_when_not_enforced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Token has no scopes but enforcement is off → middleware lets it through
        client, token = self._make_client_with_scopes(monkeypatch, scopes=[], enforce=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        # Middleware passes; MCP layer may return any non-auth status
        assert resp.status_code != 401
        assert resp.status_code != 403

