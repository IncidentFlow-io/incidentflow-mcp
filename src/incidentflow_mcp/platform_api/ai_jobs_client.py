from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings

logger = logging.getLogger(__name__)


class PlatformAPIJobsClient:
    """Thin client for MCP async orchestration against platform-api."""

    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for async MCP orchestration")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._jobs_path = settings.platform_api_ai_jobs_path
        self._timeout = settings.platform_api_timeout_seconds
        self._internal_api_key = (
            settings.platform_api_internal_api_key.get_secret_value()
            if settings.platform_api_internal_api_key
            else None
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._internal_api_key:
            headers["X-Internal-Api-Key"] = self._internal_api_key
        return headers

    async def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}{self._jobs_path}",
                json=payload,
                headers=self._headers(),
            )
        response.raise_for_status()
        return response.json()

    async def get_job(self, job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}{self._jobs_path}/{job_id}",
                headers=self._headers(),
            )
        response.raise_for_status()
        return response.json()

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}{self._jobs_path}/{job_id}/cancel",
                headers=self._headers(),
            )
        response.raise_for_status()
        return response.json()
