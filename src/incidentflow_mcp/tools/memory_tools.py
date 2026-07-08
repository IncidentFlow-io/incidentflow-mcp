"""Memory tool implementations — semantic search and upsert via platform-api.

All calls hit platform-api /internal/memory/* using the internal API key,
so the MCP server never touches Qdrant or OpenAI directly.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer, inject_trace_headers

logger = logging.getLogger(__name__)


class MemoryAPIError(Exception):
    pass


class PlatformAPIMemoryClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for memory tools")
        self._base = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._internal_key = (
            settings.platform_api_internal_api_key.get_secret_value()
            if settings.platform_api_internal_api_key
            else None
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._internal_key:
            headers["X-Internal-Api-Key"] = self._internal_key
        return headers

    async def search(
        self,
        *,
        workspace_id: str,
        query: str,
        service: str | None = None,
        limit: int = 5,
        score_threshold: float | None = None,
    ) -> dict[str, Any]:
        tracer = get_tracer()
        with tracer.start_as_current_span("qdrant.search") as span:
            span.set_attribute("memory.operation", "search")
            span.set_attribute("workspace.id", workspace_id)
            span.set_attribute("memory.limit", limit)
            if service:
                span.set_attribute("memory.service", service)

            body: dict[str, Any] = {"workspace_id": workspace_id, "query": query, "limit": limit}
            if service:
                body["service"] = service
            if score_threshold is not None:
                body["score_threshold"] = score_threshold

            headers = self._headers()
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base}/internal/memory/search",
                        json=body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    matches = result.get("matches", [])
                    span.set_attribute("memory.results_count", len(matches))
                    return result  # type: ignore[return-value]
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode

                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise

    async def upsert(
        self,
        *,
        workspace_id: str,
        incident_id: str,
        source: str,
        text: str,
        dry_run: bool = False,
        ttl_seconds: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Upsert an incident memory document via platform-api.

        With ``dry_run=True`` the payload is validated locally and returned
        without any HTTP call, so nothing is written to semantic memory.
        ``ttl_seconds`` is forwarded in the request body; actual expiry is
        enforced by platform-api (no-op until the backend supports it).
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("qdrant.upsert") as span:
            span.set_attribute("memory.operation", "upsert")
            span.set_attribute("workspace.id", workspace_id)
            span.set_attribute("memory.source", source)

            body: dict[str, Any] = {
                "workspace_id": workspace_id,
                "incident_id": incident_id,
                "source": source,
                "text": text,
                **{k: v for k, v in kwargs.items() if v is not None},
            }
            if ttl_seconds is not None:
                body["ttl_seconds"] = ttl_seconds

            if dry_run:
                span.set_attribute("memory.dry_run", True)
                missing = [
                    field
                    for field, value in (
                        ("workspace_id", workspace_id),
                        ("incident_id", incident_id),
                        ("source", source),
                        ("text", text),
                    )
                    if not (value or "").strip()
                ]
                if missing:
                    raise ValueError(
                        f"dry_run validation failed: empty fields: {', '.join(missing)}"
                    )
                return {
                    "stored": False,
                    "dry_run": True,
                    "validated": True,
                    "point_id": None,
                    "would_write": body,
                }

            headers = self._headers()
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base}/internal/memory/upsert",
                        json=body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return resp.json()  # type: ignore[return-value]
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode

                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise


async def memory_search_similar_incidents(
    settings: Settings,
    workspace_id: str,
    query: str,
    service: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    try:
        result = await client.search(
            workspace_id=workspace_id,
            query=query,
            service=service,
            limit=limit,
        )
        matches = result.get("matches", [])
        return {
            "query": query,
            "total_matches": len(matches),
            "matches": matches,
        }
    except httpx.HTTPStatusError as exc:
        logger.warning("memory search failed status=%s", exc.response.status_code)
        raise MemoryAPIError(f"Memory search failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        logger.warning("memory search error: %s", exc)
        raise MemoryAPIError(f"Memory search error: {exc}") from exc


async def memory_get_service_context(
    settings: Settings,
    workspace_id: str,
    service: str,
    query: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    # Use service name as the query when no explicit query provided
    effective_query = query or f"incident affecting {service}"
    try:
        result = await client.search(
            workspace_id=workspace_id,
            query=effective_query,
            service=service,
            limit=limit,
            score_threshold=0.3,
        )
        matches = result.get("matches", [])
        return {
            "service": service,
            "total_entries": len(matches),
            "context": matches,
        }
    except httpx.HTTPStatusError as exc:
        raise MemoryAPIError(
            f"Service context lookup failed: HTTP {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        raise MemoryAPIError(f"Service context error: {exc}") from exc


async def memory_upsert_incident_summary(
    settings: Settings,
    workspace_id: str,
    incident_id: str,
    source: str,
    text: str,
    service: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    started_at: str | None = None,
    dry_run: bool = False,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    try:
        result = await client.upsert(
            workspace_id=workspace_id,
            incident_id=incident_id,
            source=source,
            text=text,
            service=service,
            severity=severity,
            status=status,
            cluster=cluster,
            namespace=namespace,
            started_at=started_at,
            dry_run=dry_run,
            ttl_seconds=ttl_seconds,
        )
        if dry_run:
            return {
                "stored": False,
                "dry_run": True,
                "validated": bool(result.get("validated")),
                "incident_id": incident_id,
                "source": source,
                "point_id": None,
                "would_write": result.get("would_write"),
            }
        return {
            "stored": True,
            "incident_id": incident_id,
            "source": source,
            "point_id": result.get("point_id"),
            "text_hash": result.get("text_hash"),
        }
    except httpx.HTTPStatusError as exc:
        raise MemoryAPIError(f"Memory upsert failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise MemoryAPIError(f"Memory upsert error: {exc}") from exc


async def memory_find_runbook(
    settings: Settings,
    workspace_id: str,
    query: str,
    service: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    # Constrain search to runbook source by enriching the query
    runbook_query = f"runbook: {query}"
    try:
        result = await client.search(
            workspace_id=workspace_id,
            query=runbook_query,
            service=service,
            limit=limit,
        )
        matches = result.get("matches", [])
        # Filter to runbook sources if the collection has mixed sources
        runbooks = [m for m in matches if m.get("source") in ("runbook", "rca")]
        if not runbooks:
            runbooks = matches  # fall back to all results if no runbooks stored yet
        return {
            "query": query,
            "total_runbooks": len(runbooks),
            "runbooks": runbooks,
        }
    except httpx.HTTPStatusError as exc:
        raise MemoryAPIError(f"Runbook search failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise MemoryAPIError(f"Runbook search error: {exc}") from exc
