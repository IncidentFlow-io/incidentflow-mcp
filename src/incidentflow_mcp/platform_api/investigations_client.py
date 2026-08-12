"""Workspace-authenticated transport for the public investigation control API."""

from __future__ import annotations

from typing import Any

import httpx

from incidentflow_mcp.config import Settings


class PlatformInvestigationsClient:
    def __init__(self, settings: Settings, *, bearer_token: str) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for investigations")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-MCP-Client-Id": "incidentflow-mcp",
        }

    async def list(self, *, needs_attention: bool | None, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if needs_attention is not None:
            params["needs_attention"] = needs_attention
        return await self._request("GET", "/api/v1/investigations", params=params)

    async def get(self, investigation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/investigations/{investigation_id}")

    async def continue_(self, investigation_id: str, *, reason: str | None) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/v1/investigations/{investigation_id}/continue", json={"reason": reason}
        )

    async def recheck(self, investigation_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/v1/investigations/{investigation_id}/recheck", json={}
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method, f"{self._base_url}{path}", headers=self._headers, **kwargs
            )
        response.raise_for_status()
        payload = response.json()
        return dict(payload) if isinstance(payload, dict) else {}
