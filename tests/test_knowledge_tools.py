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
    memory_upsert_knowledge,
    memory_upsert_runbook,
)
from incidentflow_mcp.tools.memory_tools import _group_matches, memory_consult


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
async def test_upsert_knowledge_sends_document_id() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"point_id": "p-3", "text_hash": "h-3", "type": "knowledge", "stored": True})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        await memory_upsert_knowledge(
            s,
            workspace_id="ws-1",
            knowledge_id="6a0542d0-7ccb-4733-bdc6-286f7bf9b88f",
            title="IncidentFlow dev tools catalog",
            text="Catalog text",
        )

    body = captured[0]
    assert body["type"] == "knowledge"
    assert body["source"] == "knowledge"
    assert body["document_id"] == "6a0542d0-7ccb-4733-bdc6-286f7bf9b88f"
    assert body["text"].startswith("# IncidentFlow dev tools catalog\n\nCatalog text")
    assert "markdown" in body["tags"]
    # Legacy field is still sent until all callers have migrated.
    assert body["incident_id"] == "6a0542d0-7ccb-4733-bdc6-286f7bf9b88f"


@pytest.mark.asyncio
async def test_upsert_knowledge_preserves_existing_markdown() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp({"point_id": "p-4", "text_hash": "h-4", "type": "knowledge", "stored": True})

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    markdown = "# Existing Title\n\n## Section\n\n- item"
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        await memory_upsert_knowledge(
            s,
            workspace_id="ws-1",
            title="Different title",
            text=markdown,
            tags=["tool-review"],
        )

    body = captured[0]
    assert body["text"] == markdown
    assert body["tags"] == ["tool-review", "markdown"]


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


# ──────────────────────────────────────────────
# consult-memory (Phase 9)
# ──────────────────────────────────────────────


def test_group_matches_buckets_by_type() -> None:
    matches = [
        {"type": "runbook", "incident_id": "rb-1", "title": "RB", "score": 0.9, "service": "redis"},
        {"type": "rca", "incident_id": "rca-1", "title": "RCA", "score": 0.8},
        {"type": "incident", "incident_id": "INC-1", "title": "Inc", "score": 0.7},
        {"type": "postmortem", "incident_id": "pm-1", "title": "PM", "score": 0.6},
        {"type": "unknown_type", "incident_id": "x", "title": "X"},
    ]
    buckets = _group_matches(matches)
    assert [m["id"] for m in buckets["runbooks"]] == ["rb-1"]
    assert [m["id"] for m in buckets["rcas"]] == ["rca-1"]
    assert [m["id"] for m in buckets["similar_incidents"]] == ["INC-1"]
    assert [m["id"] for m in buckets["postmortems"]] == ["pm-1"]
    # runbook entries are compacted to the surfaced fields
    assert buckets["runbooks"][0]["service"] == "redis"


def test_group_matches_empty() -> None:
    buckets = _group_matches([])
    assert all(v == [] for v in buckets.values())


@pytest.mark.asyncio
async def test_memory_consult_single_search_and_grouping() -> None:
    s = _make_settings()
    captured: list[dict[str, Any]] = []
    resp = _resp(
        {
            "matches": [
                {"type": "runbook", "incident_id": "rb-1", "title": "RB", "score": 0.9},
                {"type": "rca", "incident_id": "rca-1", "title": "RCA", "score": 0.8},
            ]
        }
    )

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        ctx = await memory_consult(s, "ws-1", "redis oom", service="redis", namespace="prod")

    # A single search across all knowledge types, archived excluded.
    assert len(captured) == 1
    body = captured[0]
    assert set(body["types"]) == {"runbook", "rca", "incident", "postmortem"}
    assert body["exclude_status"] == ["archived"]
    assert body["service"] == "redis"
    assert body["namespace"] == "prod"
    assert ctx is not None
    assert ctx["total"] == 2
    assert ctx["runbooks"][0]["id"] == "rb-1"
    assert ctx["rcas"][0]["id"] == "rca-1"
    # empty buckets are omitted
    assert "similar_incidents" not in ctx


@pytest.mark.asyncio
async def test_memory_consult_returns_none_when_empty() -> None:
    s = _make_settings()
    resp = _resp({"matches": []})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp):
        ctx = await memory_consult(s, "ws-1", "nothing relevant")
    assert ctx is None
