"""Unit tests for memory_tools.py — no real HTTP calls."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from incidentflow_mcp.tools.memory_tools import (
    MemoryAPIError,
    PlatformAPIMemoryClient,
    memory_find_runbook,
    memory_get_service_context,
    memory_search_similar_incidents,
    memory_upsert_incident_summary,
)


def _make_settings(*, base_url: str = "http://platform-api:8000", key: str | None = None) -> Any:
    s = MagicMock()
    s.platform_api_base_url = base_url
    s.platform_api_timeout_seconds = 5.0
    internal = MagicMock()
    internal.get_secret_value.return_value = key or "test-internal-key"
    s.platform_api_internal_api_key = internal if key else None
    return s


def _resp(data: dict[str, Any], status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


# ──────────────────────────────────────────────
# PlatformAPIMemoryClient
# ──────────────────────────────────────────────


def test_client_requires_base_url() -> None:
    s = _make_settings(base_url="")
    s.platform_api_base_url = None
    with pytest.raises(ValueError, match="PLATFORM_API_BASE_URL"):
        PlatformAPIMemoryClient(s)


def test_client_strips_trailing_slash() -> None:
    s = _make_settings(base_url="http://api:8000/")
    c = PlatformAPIMemoryClient(s)
    assert not c._base.endswith("/")


def test_client_headers_include_internal_key() -> None:
    s = _make_settings(key="my-key")
    c = PlatformAPIMemoryClient(s)
    headers = c._headers()
    assert headers["X-Internal-Api-Key"] == "my-key"


def test_client_headers_omit_key_when_none() -> None:
    s = _make_settings()
    s.platform_api_internal_api_key = None
    c = PlatformAPIMemoryClient(s)
    headers = c._headers()
    assert "X-Internal-Api-Key" not in headers


# ──────────────────────────────────────────────
# memory_search_similar_incidents
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_similar_incidents_ok() -> None:
    s = _make_settings()
    matches = [{"incident_id": "INC-1", "score": 0.95, "summary": "db down"}]
    resp = _resp({"matches": matches})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        result = await memory_search_similar_incidents(
            s, workspace_id="ws-1", query="database down", limit=3
        )

    assert result["total_matches"] == 1
    assert result["query"] == "database down"
    assert result["matches"] == matches


@pytest.mark.asyncio
async def test_search_similar_incidents_http_error() -> None:
    s = _make_settings()
    resp = _resp({}, status=503)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        with pytest.raises(MemoryAPIError, match="HTTP 503"):
            await memory_search_similar_incidents(s, workspace_id="ws-1", query="crash")


@pytest.mark.asyncio
async def test_search_empty_matches() -> None:
    s = _make_settings()
    resp = _resp({"matches": []})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        result = await memory_search_similar_incidents(s, workspace_id="ws-1", query="cpu spike")

    assert result["total_matches"] == 0
    assert result["matches"] == []


# ──────────────────────────────────────────────
# memory_get_service_context
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_service_context_uses_service_as_query_when_no_query() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"matches": []})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        result = await memory_get_service_context(
            s, workspace_id="ws-1", service="orders-service"
        )

    assert "orders-service" in captured[0]["query"]
    assert result["service"] == "orders-service"


@pytest.mark.asyncio
async def test_get_service_context_uses_provided_query() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"matches": []})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        await memory_get_service_context(
            s, workspace_id="ws-1", service="orders-service", query="latency spike"
        )

    assert captured[0]["query"] == "latency spike"


# ──────────────────────────────────────────────
# memory_upsert_incident_summary
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_incident_summary_ok() -> None:
    s = _make_settings()
    resp = _resp({"point_id": "uuid-123", "text_hash": "deadbeef"})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        result = await memory_upsert_incident_summary(
            s,
            workspace_id="ws-1",
            incident_id="INC-042",
            source="incident_summary",
            text="The service crashed due to OOM.",
            service="billing-service",
            severity="critical",
        )

    assert result["stored"] is True
    assert result["point_id"] == "uuid-123"
    assert result["text_hash"] == "deadbeef"
    assert result["incident_id"] == "INC-042"


@pytest.mark.asyncio
async def test_upsert_incident_summary_http_error() -> None:
    s = _make_settings()
    resp = _resp({}, status=500)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        with pytest.raises(MemoryAPIError, match="HTTP 500"):
            await memory_upsert_incident_summary(
                s, workspace_id="ws-1", incident_id="INC-042", source="rca", text="crash"
            )


# ──────────────────────────────────────────────
# memory_find_runbook
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_runbook_filters_runbook_source() -> None:
    s = _make_settings()
    matches = [
        {"incident_id": "INC-1", "source": "runbook", "summary": "Restart service"},
        {"incident_id": "INC-2", "source": "incident_summary", "summary": "Something else"},
    ]
    resp = _resp({"matches": matches})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        result = await memory_find_runbook(s, workspace_id="ws-1", query="restart procedure")

    # Should filter to runbook/rca sources only
    assert result["total_runbooks"] == 1
    assert result["runbooks"][0]["source"] == "runbook"


@pytest.mark.asyncio
async def test_find_runbook_falls_back_to_all_when_no_runbooks() -> None:
    s = _make_settings()
    matches = [
        {"incident_id": "INC-1", "source": "incident_summary", "summary": "Something"},
    ]
    resp = _resp({"matches": matches})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        result = await memory_find_runbook(s, workspace_id="ws-1", query="any query")

    # Falls back to all results
    assert result["total_runbooks"] == 1


@pytest.mark.asyncio
async def test_find_runbook_enriches_query_with_runbook_prefix() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"matches": []})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        await memory_find_runbook(s, workspace_id="ws-1", query="redis restart")

    assert captured[0]["query"].startswith("runbook:")
