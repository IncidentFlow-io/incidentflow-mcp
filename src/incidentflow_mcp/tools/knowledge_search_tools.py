"""Unified IncidentFlow knowledge search tool."""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer, inject_trace_headers

logger = logging.getLogger(__name__)

KnowledgeScope = Literal["public", "workspace", "combined"]


class KnowledgeSearchAPIError(Exception):
    pass


class PlatformAPIKnowledgeClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for knowledge search")
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

    async def search(
        self,
        *,
        workspace_id: str | None,
        query: str,
        scope: KnowledgeScope = "combined",
        document_type: str | None = None,
        service: str | None = None,
        environment: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        tracer = get_tracer()
        with tracer.start_as_current_span("knowledge.search") as span:
            span.set_attribute("knowledge.scope", scope)
            span.set_attribute("knowledge.limit", limit)
            if workspace_id:
                span.set_attribute("workspace.id", workspace_id)
            body: dict[str, Any] = {
                "query": query,
                "scope": scope,
                "limit": limit,
            }
            if workspace_id:
                body["workspace_id"] = workspace_id
            if document_type:
                body["document_type"] = document_type
            if service:
                body["service"] = service
            if environment:
                body["environment"] = environment

            headers = self._headers()
            inject_trace_headers(headers)
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base}/internal/knowledge/search",
                    json=body,
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                span.set_attribute(
                    "knowledge.workspace_results_count",
                    len(payload.get("workspaceResults", [])),
                )
                span.set_attribute(
                    "knowledge.public_results_count",
                    len(payload.get("publicResults", [])),
                )
                return payload  # type: ignore[return-value]


async def incidentflow_knowledge_search(
    settings: Settings,
    *,
    workspace_id: str | None,
    query: str,
    scope: KnowledgeScope = "combined",
    document_type: str | None = None,
    service: str | None = None,
    environment: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    client = PlatformAPIKnowledgeClient(settings)
    try:
        return await client.search(
            workspace_id=workspace_id,
            query=query,
            scope=scope,
            document_type=document_type,
            service=service,
            environment=environment,
            limit=limit,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("knowledge search failed status=%s", exc.response.status_code)
        raise KnowledgeSearchAPIError(
            f"IncidentFlow knowledge search failed: HTTP {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        logger.warning("knowledge search error: %s", exc)
        raise KnowledgeSearchAPIError(f"IncidentFlow knowledge search error: {exc}") from exc
