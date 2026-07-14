from __future__ import annotations

from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api.grafana_client import _raise_for_status_with_body

_BASE_PATH = "/internal/integrations/argocd"


class PlatformArgoCDClient:
    """Argo CD read client backed by platform-api internal endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        workspace_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for Argo CD platform mode")
        token = settings.platform_api_internal_api_key
        if token is None:
            raise ValueError("PLATFORM_API_INTERNAL_TOKEN is required for Argo CD platform mode")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._workspace_id = workspace_id
        self._transport = transport
        self._headers = {
            "X-Internal-Api-Key": token.get_secret_value(),
            "X-MCP-Client-Id": "incidentflow-mcp",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"workspace_id": self._workspace_id, **payload}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(
                f"{self._base_url}{path}", json=body, headers=self._headers
            )
        _raise_for_status_with_body(response)
        return dict(response.json())

    async def health(self, *, integration_id: str | None = None) -> dict[str, Any]:
        return await self._post(f"{_BASE_PATH}/health", _integration_payload(integration_id))

    async def list_applications(
        self,
        *,
        integration_id: str | None = None,
        search: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        destination_cluster: str | None = None,
        health_status: str | None = None,
        sync_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/applications",
            _drop_none(
                {
                    "integration_id": integration_id,
                    "search": search,
                    "project": project,
                    "namespace": namespace,
                    "destination_cluster": destination_cluster,
                    "health_status": health_status,
                    "sync_status": sync_status,
                    "limit": limit,
                }
            ),
        )

    async def get_application(
        self,
        *,
        name: str,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/application",
            _drop_none({"integration_id": integration_id, "name": name}),
        )

    async def get_application_resources(
        self,
        *,
        name: str,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/application/resources",
            _drop_none({"integration_id": integration_id, "name": name}),
        )

    async def get_sync_history(
        self,
        *,
        name: str,
        integration_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/application/history",
            _drop_none({"integration_id": integration_id, "name": name, "limit": limit}),
        )

    async def get_last_operation(
        self,
        *,
        name: str,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/application/operation",
            _drop_none({"integration_id": integration_id, "name": name}),
        )

    async def find_recent_deployments(
        self,
        *,
        integration_id: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/deployments",
            _drop_none(
                {
                    "integration_id": integration_id,
                    "project": project,
                    "namespace": namespace,
                    "limit": limit,
                }
            ),
        )

    async def analyze_application(
        self,
        *,
        name: str,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"{_BASE_PATH}/application/analyze",
            _drop_none({"integration_id": integration_id, "name": name}),
        )


def _integration_payload(integration_id: str | None) -> dict[str, str]:
    return {"integration_id": integration_id} if integration_id else {}


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
