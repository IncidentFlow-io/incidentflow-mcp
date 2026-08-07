from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentflow_mcp.auth.middleware import BearerAuthMiddleware
from incidentflow_mcp.auth.oauth import OAuthValidationResult
from incidentflow_mcp.auth.repository import InMemoryTokenRepository
from incidentflow_mcp.config import Settings


class FakeIntrospectionClient:
    def __init__(
        self,
        *,
        active: Callable[[], bool],
        requests: list[tuple[str, dict[str, str], dict[str, str]]],
        timeout: float,
    ) -> None:
        self._active = active
        self._requests = requests
        self.timeout = timeout

    async def __aenter__(self) -> "FakeIntrospectionClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, str],
    ) -> httpx.Response:
        self._requests.append((url, headers, data))
        return httpx.Response(200, json={"active": self._active()})


def _oauth_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware)

    @app.get("/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    return app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        oauth_expected_issuer="https://auth.incidentflow.test",
        oauth_jwks_url="https://auth.incidentflow.test/.well-known/jwks.json",
        platform_api_base_url="https://platform.incidentflow.test",
        oauth_introspection_api_key="internal-revocation-key",
        platform_api_timeout_seconds=0.25,
        log_level="warning",
    )


def test_locally_valid_oauth_token_is_denied_immediately_after_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

    async def locally_valid(**kwargs: object) -> OAuthValidationResult:
        _ = kwargs
        return OAuthValidationResult(
            ok=True,
            code="ok",
            detail="ok",
            claims={"client_id": "client-1", "jti": "access-jti-1"},
        )

    monkeypatch.setattr(
        "incidentflow_mcp.auth.middleware.validate_oauth_access_token",
        locally_valid,
    )

    status = {"active": True}
    requests: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def client_factory(*args: object, **kwargs: Any) -> FakeIntrospectionClient:
        _ = args
        return FakeIntrospectionClient(
            active=lambda: status["active"],
            requests=requests,
            timeout=float(kwargs["timeout"]),
        )

    monkeypatch.setattr("incidentflow_mcp.auth.middleware.httpx.AsyncClient", client_factory)
    client = TestClient(_oauth_app(), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer locally.valid.jwt"}

    first_use = client.get("/private", headers=headers)
    assert first_use.status_code == 200

    status["active"] = False
    reuse_after_revoke = client.get("/private", headers=headers)
    assert reuse_after_revoke.status_code == 401
    assert "inactive or revoked" in reuse_after_revoke.json()["detail"]

    # One authority lookup per use proves there is no positive-status cache.
    assert len(requests) == 2
    assert requests[0][0] == "https://platform.incidentflow.test/oauth/introspect"
    assert requests[0][1]["X-Internal-Api-Key"] == "internal-revocation-key"
    assert requests[0][2] == {"token": "locally.valid.jwt"}


def test_oauth_revocation_authority_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("incidentflow_mcp.config._settings", _settings())
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

    async def locally_valid(**kwargs: object) -> OAuthValidationResult:
        _ = kwargs
        return OAuthValidationResult(ok=True, code="ok", detail="ok", claims={"jti": "jti-1"})

    class TimeoutClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        async def __aenter__(self) -> "TimeoutClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            _ = args, kwargs
            raise httpx.ConnectTimeout("authority timeout")

    monkeypatch.setattr(
        "incidentflow_mcp.auth.middleware.validate_oauth_access_token",
        locally_valid,
    )
    monkeypatch.setattr("incidentflow_mcp.auth.middleware.httpx.AsyncClient", TimeoutClient)

    response = TestClient(_oauth_app(), raise_server_exceptions=False).get(
        "/private",
        headers={"Authorization": "Bearer locally.valid.jwt"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Token revocation service unavailable"


def test_production_oauth_without_authority_url_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings().model_copy(update={"platform_api_base_url": None})
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

    async def locally_valid(**kwargs: object) -> OAuthValidationResult:
        _ = kwargs
        return OAuthValidationResult(ok=True, code="ok", detail="ok", claims={"jti": "jti-1"})

    monkeypatch.setattr(
        "incidentflow_mcp.auth.middleware.validate_oauth_access_token",
        locally_valid,
    )

    response = TestClient(_oauth_app(), raise_server_exceptions=False).get(
        "/private",
        headers={"Authorization": "Bearer locally.valid.jwt"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Token revocation service is not configured"


def test_locally_invalid_oauth_token_never_reaches_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("incidentflow_mcp.config._settings", _settings())
    monkeypatch.setattr("incidentflow_mcp.auth.repository._repo", InMemoryTokenRepository())

    async def locally_invalid(**kwargs: object) -> OAuthValidationResult:
        _ = kwargs
        return OAuthValidationResult(
            ok=False,
            code="oauth_invalid",
            detail="Invalid token audience/resource",
        )

    def client_must_not_be_created(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError("introspection must run only after local JWT validation")

    monkeypatch.setattr(
        "incidentflow_mcp.auth.middleware.validate_oauth_access_token",
        locally_invalid,
    )
    monkeypatch.setattr(
        "incidentflow_mcp.auth.middleware.httpx.AsyncClient",
        client_must_not_be_created,
    )

    response = TestClient(_oauth_app(), raise_server_exceptions=False).get(
        "/private",
        headers={"Authorization": "Bearer locally.invalid.jwt"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token audience/resource"
