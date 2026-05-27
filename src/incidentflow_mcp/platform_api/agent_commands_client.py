from __future__ import annotations

from typing import Any

import httpx

from incidentflow_mcp.config import Settings


class PlatformAPIAgentCommandsClient:
    """Thin client for Kubernetes agent command dispatch through platform-api."""

    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for Kubernetes agent tools")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds

    async def list_clusters(self, *, bearer_token: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/agents/clusters",
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
        response.raise_for_status()
        payload = response.json()
        clusters = payload.get("clusters") if isinstance(payload, dict) else None
        return clusters if isinstance(clusters, list) else []

    async def dispatch(
        self,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": action,
            "params": params,
        }
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds

        async with httpx.AsyncClient(timeout=self._timeout + (timeout_seconds or 0)) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/agents/clusters/{cluster_id}/commands",
                headers={"Authorization": f"Bearer {bearer_token}"},
                json=payload,
            )
        response.raise_for_status()
        return response.json()
