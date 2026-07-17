from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

from incidentflow_mcp.tools.knowledge_search_tools import (
    PlatformAPIKnowledgeClient,
    incidentflow_knowledge_search,
)


def _settings() -> Any:
    class S:
        platform_api_base_url = "http://platform-api:8000"
        platform_api_timeout_seconds = 5.0
        platform_api_internal_api_key = SecretStr("internal")

    return S()


@pytest.mark.asyncio
async def test_platform_knowledge_client_search_uses_internal_endpoint() -> None:
    captured: dict[str, Any] = {}

    async def post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "query": "checkout latency",
                "scope": "combined",
                "workspaceResults": [{"title": "Checkout RCA"}],
                "publicResults": [{"title": "Latency runbook"}],
            },
        )

    with patch("incidentflow_mcp.tools.knowledge_search_tools.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post.side_effect = post
        client_cls.return_value.__aenter__.return_value = client

        result = await PlatformAPIKnowledgeClient(_settings()).search(
            workspace_id="ws-1",
            query="checkout latency",
            scope="combined",
            document_type="rca",
            service="checkout-api",
            environment="production",
            limit=8,
        )

    assert captured["url"] == "http://platform-api:8000/internal/knowledge/search"
    assert captured["json"] == {
        "workspace_id": "ws-1",
        "query": "checkout latency",
        "scope": "combined",
        "limit": 8,
        "response_mode": "compact",
        "document_type": "rca",
        "service": "checkout-api",
        "environment": "production",
    }
    assert captured["headers"]["X-Internal-Api-Key"] == "internal"
    assert result["workspaceResults"][0]["title"] == "Checkout RCA"
    assert result["publicResults"][0]["title"] == "Latency runbook"


@pytest.mark.asyncio
async def test_incidentflow_knowledge_search_returns_separated_results() -> None:
    payload = {
        "query": "mcp install",
        "scope": "public",
        "workspaceResults": [],
        "publicResults": [{"title": "Installation"}],
    }
    with patch.object(PlatformAPIKnowledgeClient, "search", AsyncMock(return_value=payload)):
        result = await incidentflow_knowledge_search(
            _settings(),
            workspace_id="ws-1",
            query="mcp install",
            scope="public",
        )

    assert result == payload


@pytest.mark.asyncio
async def test_platform_knowledge_client_omits_workspace_for_public_scope() -> None:
    captured: dict[str, Any] = {}

    async def post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        _ = headers
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "query": "install",
                "scope": "public",
                "workspaceResults": [],
                "publicResults": [],
            },
        )

    with patch("incidentflow_mcp.tools.knowledge_search_tools.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post.side_effect = post
        client_cls.return_value.__aenter__.return_value = client

        await PlatformAPIKnowledgeClient(_settings()).search(
            workspace_id=None,
            query="install",
            scope="public",
            limit=5,
        )

    assert captured["url"] == "http://platform-api:8000/internal/knowledge/search"
    assert captured["json"] == {
        "query": "install",
        "scope": "public",
        "limit": 5,
        "response_mode": "compact",
    }
