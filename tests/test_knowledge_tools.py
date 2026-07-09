"""Unit tests for knowledge_tools.py — typed upsert/find, no real HTTP calls."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from incidentflow_mcp.tools.knowledge_tools import (
    _slugify,
    memory_find_knowledge,
    memory_find_rca,
    memory_upsert_incident,
    memory_upsert_runbook,
)


def _make_settings() -> Any:
    s = MagicMock()
    s.platform_api_base_url = "http://platform-api:8000"
    s.platform_api_timeout_seconds = 5.0
    internal = MagicMock()
    internal.get_secret_value.return_value = "test-internal-key"
    s.platform_api_internal_api_key = internal
    return s


def _resp(data: dict[str, Any], status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


def test_slugify_generates_stable_id() -> None:
    assert _slugify("Kubernetes StartupProbe HTTP 500", prefix="runbook") == (
        "runbook-kubernetes-startupprobe-http-500"
    )


@pytest.mark.asyncio
async def test_upsert_runbook_sets_type_and_derives_id() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"point_id": "p-1", "text_hash": "h-1", "type": "runbook", "stored": True})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        result = await memory_upsert_runbook(
            s,
            workspace_id="ws-1",
            title="StartupProbe HTTP 500",
            text="Symptoms...",
            service="platform-api",
            tags=["kubernetes", "http500"],
        )

    body = captured[0]
    assert body["type"] == "runbook"
    assert body["source"] == "runbook"
    # runbook_id omitted → derived from title
    assert body["incident_id"] == "runbook-startupprobe-http-500"
    assert body["title"] == "StartupProbe HTTP 500"
    assert body["tags"] == ["kubernetes", "http500"]
    assert result["stored"] is True
    assert result["type"] == "runbook"
    assert result["point_id"] == "p-1"


@pytest.mark.asyncio
async def test_upsert_runbook_dry_run_does_not_write() -> None:
    s = _make_settings()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        result = await memory_upsert_runbook(
            s,
            workspace_id="ws-1",
            title="Restart pod",
            text="kubectl rollout restart",
            dry_run=True,
        )
    mock_post.assert_not_called()
    assert result["stored"] is False
    assert result["dry_run"] is True
    assert result["validated"] is True
    assert result["would_write"]["type"] == "runbook"


@pytest.mark.asyncio
async def test_upsert_incident_requires_explicit_id() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"point_id": "p-2", "text_hash": "h-2", "type": "incident", "stored": True})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        await memory_upsert_incident(
            s,
            workspace_id="ws-1",
            incident_id="INC-77",
            title="DB outage",
            text="connections exhausted",
        )

    body = captured[0]
    assert body["type"] == "incident"
    assert body["source"] == "incident_summary"
    assert body["incident_id"] == "INC-77"


@pytest.mark.asyncio
async def test_find_rca_filters_by_type() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"matches": [{"incident_id": "INC-1", "type": "rca"}]})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        result = await memory_find_rca(s, workspace_id="ws-1", query="node OOM")

    assert captured[0]["types"] == ["rca"]
    assert captured[0]["exclude_status"] == ["archived"]
    assert result["total_rcas"] == 1


@pytest.mark.asyncio
async def test_find_knowledge_filters_by_type() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"matches": []})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        result = await memory_find_knowledge(s, workspace_id="ws-1", query="istio mtls")

    assert captured[0]["types"] == ["knowledge"]
    assert result["total_knowledge"] == 0
