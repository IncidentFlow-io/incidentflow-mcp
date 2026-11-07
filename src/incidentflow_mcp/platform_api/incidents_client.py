"""Workspace-authenticated reads for canonical incident summaries."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from incidentflow_mcp.config import Settings


class PlatformIncidentsClient:
    def __init__(self, settings: Settings, *, bearer_token: str) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for incident reads")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-MCP-Client-Id": "incidentflow-mcp",
        }

    async def get(self, incident_id: str) -> dict[str, Any]:
        return await self._request("/api/v1/incidents/" + quote(incident_id, safe=""))

    async def get_timeline(self, incident_id: str) -> dict[str, Any]:
        return await self._request(
            "/api/v1/incidents/" + quote(incident_id, safe="") + "/timeline"
        )

    async def _request(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}{path}", headers=self._headers)
        response.raise_for_status()
        payload = response.json()
        return dict(payload) if isinstance(payload, dict) else {}
