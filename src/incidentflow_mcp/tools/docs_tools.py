"""Public IncidentFlow documentation search tool."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer, inject_trace_headers

logger = logging.getLogger(__name__)


class DocsSearchAPIError(Exception):
    pass


class PlatformAPIDocsClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for docs search")
        self._base = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._internal_key = (
            settings.platform_api_internal_api_key.get_secret_value()
            if settings.platform_api_internal_api_key
            else None
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._internal_key:
            headers["X-Internal-Api-Key"] = self._internal_key
        return headers

    async def search(self, *, query: str, limit: int = 5) -> dict[str, Any]:
        tracer = get_tracer()
        with tracer.start_as_current_span("public_docs.search") as span:
            span.set_attribute("docs.limit", limit)
            headers = self._headers()
            inject_trace_headers(headers)
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base}/internal/docs/search",
                    json={"query": query, "limit": limit},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                span.set_attribute("docs.results_count", len(payload.get("matches", [])))
                return payload  # type: ignore[return-value]


async def incidentflow_docs_search(
    settings: Settings,
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    client = PlatformAPIDocsClient(settings)
    try:
        result = await client.search(query=query, limit=limit)
        matches = result.get("matches", [])
        return {
            "query": query,
            "total_matches": len(matches),
            "matches": matches,
        }
    except httpx.HTTPStatusError as exc:
        logger.warning("public docs search failed status=%s", exc.response.status_code)
        raise DocsSearchAPIError(
            f"IncidentFlow docs search failed: HTTP {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        logger.warning("public docs search error: %s", exc)
        raise DocsSearchAPIError(f"IncidentFlow docs search error: {exc}") from exc
