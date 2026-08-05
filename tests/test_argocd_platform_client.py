"""Tests for the MCP-side Argo CD platform client (httpx.MockTransport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api.argocd_client import PlatformArgoCDClient

WORKSPACE_ID = "ws-argocd"


def _settings() -> Settings:
    return Settings(
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="secret-key",
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response], captured: list[httpx.Request]
) -> PlatformArgoCDClient:
    def _recording(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return PlatformArgoCDClient(
        _settings(), workspace_id=WORKSPACE_ID, transport=httpx.MockTransport(_recording)
    )


def _json(payload: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, json=payload)


class TestConstruction:
    def test_requires_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLATFORM_API_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="PLATFORM_API_BASE_URL"):
            PlatformArgoCDClient(
                Settings(_env_file=None, platform_api_internal_api_key="k"),
                workspace_id=WORKSPACE_ID,
            )

    def test_requires_internal_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLATFORM_API_INTERNAL_TOKEN", raising=False)
        monkeypatch.delenv("PLATFORM_API_INTERNAL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="PLATFORM_API_INTERNAL_TOKEN"):
            PlatformArgoCDClient(
                Settings(_env_file=None, platform_api_base_url="http://platform.test"),
                workspace_id=WORKSPACE_ID,
            )


class TestReadMethods:
    async def test_health_posts_workspace_and_headers(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"source": {"type": "argocd"}}), captured)

        await client.health()

        req = captured[0]
        assert req.method == "POST"
        assert req.url.path == "/internal/integrations/argocd/health"
        assert req.headers["X-Internal-Api-Key"] == "secret-key"
        assert req.headers["X-MCP-Client-Id"] == "incidentflow-mcp"
        assert json.loads(req.content) == {"workspace_id": WORKSPACE_ID}

    async def test_list_applications_omits_none_filters(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"applications": [], "source": {"type": "argocd"}}), captured)

        await client.list_applications(search="api", sync_status="OutOfSync", limit=10)

        body = json.loads(captured[0].content)
        assert captured[0].url.path == "/internal/integrations/argocd/applications"
        assert body == {
            "workspace_id": WORKSPACE_ID,
            "search": "api",
            "sync_status": "OutOfSync",
            "limit": 10,
        }

    async def test_application_methods_use_read_paths(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"source": {"type": "argocd"}}), captured)

        await client.get_application(name="checkout")
        await client.get_application_resources(name="checkout")
        await client.get_sync_history(name="checkout", limit=5)
        await client.get_last_operation(name="checkout")
        await client.find_recent_deployments(project="default", limit=3)
        await client.analyze_application(name="checkout")

        assert [req.url.path for req in captured] == [
            "/internal/integrations/argocd/application",
            "/internal/integrations/argocd/application/resources",
            "/internal/integrations/argocd/application/history",
            "/internal/integrations/argocd/application/operation",
            "/internal/integrations/argocd/deployments",
            "/internal/integrations/argocd/application/analyze",
        ]
        assert json.loads(captured[2].content)["limit"] == 5

    async def test_http_error_includes_platform_error_body(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(
            _json(
                {
                    "code": "not_found",
                    "message": "Argo CD integration is not configured",
                    "request_id": "req-123",
                },
                status=404,
            ),
            captured,
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.health()

        message = str(exc_info.value)
        assert "code=not_found" in message
        assert "message=Argo CD integration is not configured" in message
        assert "request_id=req-123" in message
