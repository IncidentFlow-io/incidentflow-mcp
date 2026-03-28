from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.metrics import mcp_platform_api_jobs_errors_total, pod_label_values

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
        self._namespace, self._pod = pod_label_values()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._internal_api_key:
            headers["X-Internal-Api-Key"] = self._internal_api_key
        return headers

    def _observe_error(self, operation: str, exc: Exception) -> None:
        status_code = "transport"
        error_type = type(exc).__name__

        if isinstance(exc, httpx.HTTPStatusError):
            status_code = str(exc.response.status_code)
        elif isinstance(exc, httpx.TimeoutException):
            status_code = "timeout"
        elif isinstance(exc, httpx.RequestError):
            status_code = "request_error"

        mcp_platform_api_jobs_errors_total.labels(
            namespace=self._namespace,
            pod=self._pod,
            operation=operation,
            status_code=status_code,
            error_type=error_type,
        ).inc()

    async def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}{self._jobs_path}",
                    json=payload,
                    headers=self._headers(),
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self._observe_error("submit_job", exc)
            raise

    async def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}{self._jobs_path}/{job_id}",
                    headers=self._headers(),
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self._observe_error("get_job", exc)
            raise

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}{self._jobs_path}/{job_id}/cancel",
                    headers=self._headers(),
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self._observe_error("cancel_job", exc)
            raise
