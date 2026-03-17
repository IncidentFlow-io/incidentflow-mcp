from __future__ import annotations

from types import SimpleNamespace

import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import _resolve_execution_mode
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient


def test_resolve_execution_mode_auto_sync_in_dev() -> None:
    settings = Settings(_env_file=None, environment="development", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "sync"


def test_resolve_execution_mode_auto_async_in_production() -> None:
    settings = Settings(_env_file=None, environment="production", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "async"


@pytest.mark.asyncio
async def test_platform_api_jobs_client_submit_includes_internal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self._timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict, headers: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured["key"] = headers.get("X-Internal-Api-Key", "")
            captured["job_type"] = json["job_type"]
            return FakeResponse({"job_id": "job_123", "status": "queued", "created_at": "2026-01-01T00:00:00Z"})

    monkeypatch.setattr("incidentflow_mcp.platform_api.ai_jobs_client.httpx.AsyncClient", FakeAsyncClient)

    settings = Settings(
        _env_file=None,
        environment="test",
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="secret-key",
    )
    client = PlatformAPIJobsClient(settings)
    payload = {"job_type": "incident.summary.generate"}
    response = await client.submit_job(payload)

    assert response["job_id"] == "job_123"
    assert captured["url"] == "http://platform.test/api/v1/ai/jobs"
    assert captured["key"] == "secret-key"
    assert captured["job_type"] == "incident.summary.generate"
