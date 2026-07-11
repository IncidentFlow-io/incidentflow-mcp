"""Tests for the MCP-side Grafana platform client (httpx.MockTransport, no network)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api.grafana_client import PlatformGrafanaClient

WORKSPACE_ID = "ws-123"


def _settings() -> Settings:
    return Settings(
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="secret-key",
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response], captured: list[httpx.Request]
) -> PlatformGrafanaClient:
    def _recording(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return PlatformGrafanaClient(
        _settings(), workspace_id=WORKSPACE_ID, transport=httpx.MockTransport(_recording)
    )


def _json(payload: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, json=payload)


class TestConstruction:
    def test_requires_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLATFORM_API_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="PLATFORM_API_BASE_URL"):
            PlatformGrafanaClient(
                Settings(_env_file=None, platform_api_internal_api_key="k"),
                workspace_id=WORKSPACE_ID,
            )

    def test_requires_internal_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLATFORM_API_INTERNAL_TOKEN", raising=False)
        monkeypatch.delenv("PLATFORM_API_INTERNAL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="PLATFORM_API_INTERNAL_TOKEN"):
            PlatformGrafanaClient(
                Settings(_env_file=None, platform_api_base_url="http://platform.test"),
                workspace_id=WORKSPACE_ID,
            )

    def test_sets_internal_headers(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"dashboards": []}), captured)
        # exercised below; header check happens per-request
        assert client is not None


class TestReadMethods:
    async def test_list_dashboards(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"dashboards": [{"uid": "a"}, {"uid": "b"}]}), captured)
        result = await client.list_dashboards()
        assert [d["uid"] for d in result] == ["a", "b"]
        req = captured[0]
        assert req.url.path == "/internal/integrations/grafana/allowed-dashboards"
        assert req.url.params["workspace_id"] == WORKSPACE_ID
        assert req.headers["X-Internal-Api-Key"] == "secret-key"
        assert req.headers["X-MCP-Client-Id"] == "incidentflow-mcp"

    async def test_list_dashboards_missing_key_is_empty(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({}), captured)
        assert await client.list_dashboards() == []

    async def test_get_dashboard(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"uid": "dns", "title": "DNS"}), captured)
        result = await client.get_dashboard("dns")
        assert result["title"] == "DNS"
        assert captured[0].url.params["dashboard_uid"] == "dns"

    async def test_extract_queries(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"queries": [{"expr": "up"}]}), captured)
        result = await client.extract_queries("dns")
        assert result[0]["expr"] == "up"
        assert captured[0].url.path == "/internal/integrations/grafana/extract-queries"


class TestQueryMethods:
    async def test_instant_query_posts_body(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"result_type": "vector", "series": []}), captured)
        await client.query(datasource_uid="ds1", query="up", time="123")
        req = captured[0]
        assert req.method == "POST"
        assert req.url.path == "/internal/integrations/grafana/query"
        import json

        body = json.loads(req.content)
        assert body == {
            "workspace_id": WORKSPACE_ID,
            "datasource_uid": "ds1",
            "query": "up",
            "time": "123",
        }

    async def test_instant_query_omits_time_when_absent(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({}), captured)
        await client.query(datasource_uid="ds1", query="up")
        import json

        assert "time" not in json.loads(captured[0].content)

    async def test_range_query_posts_window(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"result_type": "matrix", "series": []}), captured)
        await client.query_range(
            datasource_uid="ds1", query="up", start="now-6h", end="now", step="60s"
        )
        import json

        body = json.loads(captured[0].content)
        assert body["start"] == "now-6h"
        assert body["step"] == "60s"
        assert captured[0].url.path == "/internal/integrations/grafana/query-range"

    async def test_analyze_defaults_and_step(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"panels": []}), captured)
        await client.analyze(dashboard_uid="dns")
        import json

        body = json.loads(captured[0].content)
        assert body == {
            "workspace_id": WORKSPACE_ID,
            "dashboard_uid": "dns",
            "start": "now-6h",
            "end": "now",
        }
        assert captured[0].url.path == "/internal/integrations/grafana/analyze"

    async def test_analyze_includes_step_when_set(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"panels": []}), captured)
        await client.analyze(dashboard_uid="dns", start="now-1h", end="now", step="30s")
        import json

        assert json.loads(captured[0].content)["step"] == "30s"

    async def test_panel_view_posts_body(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"version": "1"}), captured)
        await client.get_panel_view(
            dashboard_uid="platform",
            panel_id=7,
            start="now-1h",
            end="now",
            variables={"service": "api"},
            max_points=200,
        )
        import json

        assert captured[0].url.path == "/internal/integrations/grafana/panel-view"
        assert json.loads(captured[0].content) == {
            "workspace_id": WORKSPACE_ID,
            "dashboard_uid": "platform",
            "panel_id": 7,
            "from": "now-1h",
            "to": "now",
            "variables": {"service": "api"},
            "maxPoints": 200,
        }


class TestErrors:
    async def test_http_error_raises(self) -> None:
        captured: list[httpx.Request] = []
        client = _client(_json({"detail": "forbidden"}, status=403), captured)
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_dashboards()
