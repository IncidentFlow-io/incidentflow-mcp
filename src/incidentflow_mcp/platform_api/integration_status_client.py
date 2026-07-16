from __future__ import annotations

from typing import Any, Literal

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer, inject_trace_headers

IntegrationStatusEndpoint = Literal["slack", "grafana", "argocd"]

_STATUS_PATHS: dict[IntegrationStatusEndpoint, str] = {
    "slack": "/api/v1/integrations/slack/status",
    "grafana": "/api/v1/integrations/grafana/status",
    "argocd": "/api/v1/integrations/argocd",
}


class PlatformIntegrationStatusClient:
    """Customer-facing integration status client backed by platform-api."""

    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for integration status")
        self._settings = settings
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds

    async def get_status(
        self,
        integration: IntegrationStatusEndpoint,
        *,
        bearer_token: str,
    ) -> dict[str, Any]:
        path = _STATUS_PATHS[integration]
        url = f"{self._base_url}{path}"
        tracer = get_tracer()
        with tracer.start_as_current_span(f"platform_api.{integration}_status") as span:
            span.set_attribute("http.method", "GET")
            span.set_attribute("http.url", url)
            headers = {"Authorization": f"Bearer {bearer_token}"}
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("response.bytes", len(response.content))
                span.set_attribute(
                    "integration.connected",
                    bool(payload.get("connected")) if isinstance(payload, dict) else False,
                )
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode

                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise

    async def get_workspace_status(self, *, workspace_id: str) -> dict[str, Any]:
        internal_key = self._settings.platform_api_internal_api_key
        if internal_key is None:
            raise ValueError("PLATFORM_API_INTERNAL_TOKEN is required for workspace status")

        url = f"{self._base_url}/internal/integrations/status/workspace"
        tracer = get_tracer()
        with tracer.start_as_current_span("platform_api.workspace_integrations_status") as span:
            span.set_attribute("http.method", "GET")
            span.set_attribute("http.url", url)
            span.set_attribute("workspace.id", workspace_id)
            headers = {"X-Internal-Api-Key": internal_key.get_secret_value()}
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(
                        url,
                        headers=headers,
                        params={"workspace_id": workspace_id},
                    )
                response.raise_for_status()
                payload = response.json()
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("response.bytes", len(response.content))
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode

                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise
