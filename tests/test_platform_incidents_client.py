from __future__ import annotations

import httpx
import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api.incidents_client import PlatformIncidentsClient


@pytest.mark.asyncio
async def test_incident_client_reads_resolved_incident_with_workspace_bearer(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        platform_api_base_url="https://platform.example/",
        platform_api_timeout_seconds=5,
    )
    expected = {"id": "incident-1", "status": "resolved"}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return expected

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> Response:
            assert url == "https://platform.example/api/v1/incidents/incident-1"
            assert headers["Authorization"] == "Bearer workspace-scoped-token"
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *, timeout: Client())
    client = PlatformIncidentsClient(settings, bearer_token="workspace-scoped-token")

    result = await client.get("incident-1")

    assert result == expected
