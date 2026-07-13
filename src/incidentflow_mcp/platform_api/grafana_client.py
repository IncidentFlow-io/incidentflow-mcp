from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings

logger = logging.getLogger(__name__)

_BASE_PATH = "/internal/integrations/grafana"


class PlatformGrafanaClient:
    """Grafana read client backed by platform-api internal endpoints.

    Thin transport over ``/internal/integrations/grafana/*`` — all guardrails
    (dashboard allow-list, PromQL safety, label sanitization) and the metric
    normalization happen server-side in platform-api. The MCP layer only
    relays. Mirrors :class:`PlatformSlackClient`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        workspace_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for Grafana platform mode")
        token = settings.platform_api_internal_api_key
        if token is None:
            raise ValueError("PLATFORM_API_INTERNAL_TOKEN is required for Grafana platform mode")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._workspace_id = workspace_id
        self._transport = transport
        self._headers = {
            "X-Internal-Api-Key": token.get_secret_value(),
            "X-MCP-Client-Id": "incidentflow-mcp",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.get(
                f"{self._base_url}{path}", params=params, headers=self._headers
            )
        response.raise_for_status()
        return dict(response.json())

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers
            )
        response.raise_for_status()
        return dict(response.json())

    async def list_dashboards(self) -> list[dict[str, Any]]:
        """Return the workspace's allow-listed dashboards."""
        payload = await self._get(
            f"{_BASE_PATH}/allowed-dashboards", {"workspace_id": self._workspace_id}
        )
        return list(payload.get("dashboards", []) or [])

    async def get_dashboard(self, dashboard_uid: str) -> dict[str, Any]:
        """Return a single dashboard's metadata (subject to the allow-list)."""
        return await self._get(
            f"{_BASE_PATH}/dashboard",
            {"workspace_id": self._workspace_id, "dashboard_uid": dashboard_uid},
        )

    async def extract_queries(self, dashboard_uid: str) -> list[dict[str, Any]]:
        """Return the PromQL targets extracted from a dashboard's panels."""
        payload = await self._get(
            f"{_BASE_PATH}/extract-queries",
            {"workspace_id": self._workspace_id, "dashboard_uid": dashboard_uid},
        )
        return list(payload.get("queries", []) or [])

    async def query(
        self, *, datasource_uid: str, query: str, time: str | None = None
    ) -> dict[str, Any]:
        """Run an instant PromQL query (server validates + normalizes)."""
        body: dict[str, Any] = {
            "workspace_id": self._workspace_id,
            "datasource_uid": datasource_uid,
            "query": query,
        }
        if time is not None:
            body["time"] = time
        return await self._post(f"{_BASE_PATH}/query", body)

    async def query_range(
        self, *, datasource_uid: str, query: str, start: str, end: str, step: str
    ) -> dict[str, Any]:
        """Run a range PromQL query (server validates + normalizes)."""
        return await self._post(
            f"{_BASE_PATH}/query-range",
            {
                "workspace_id": self._workspace_id,
                "datasource_uid": datasource_uid,
                "query": query,
                "start": start,
                "end": end,
                "step": step,
            },
        )

    async def analyze(
        self,
        *,
        dashboard_uid: str,
        start: str = "now-6h",
        end: str = "now",
        step: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a dashboard's health over a time window (server-side guarded)."""
        body: dict[str, Any] = {
            "workspace_id": self._workspace_id,
            "dashboard_uid": dashboard_uid,
            "start": start,
            "end": end,
        }
        if step is not None:
            body["step"] = step
        return await self._post(f"{_BASE_PATH}/analyze", body)

    async def get_panel_view(
        self,
        *,
        dashboard_uid: str,
        panel_id: int,
        start: str = "now-1h",
        end: str = "now",
        variables: dict[str, str | list[str]] | None = None,
        max_points: int = 300,
    ) -> dict[str, Any]:
        """Return a normalized Apps SDK Grafana panel view."""
        return await self._post(
            f"{_BASE_PATH}/panel-view",
            {
                "workspace_id": self._workspace_id,
                "dashboard_uid": dashboard_uid,
                "panel_id": panel_id,
                "from": start,
                "to": end,
                "variables": variables or {},
                "maxPoints": max_points,
            },
        )
