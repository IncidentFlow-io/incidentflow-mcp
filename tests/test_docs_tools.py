from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

from incidentflow_mcp.tools.docs_tools import PlatformAPIDocsClient, incidentflow_docs_search


def _settings() -> Any:
    class S:
        platform_api_base_url = "http://platform-api:8000"
        platform_api_timeout_seconds = 5.0
        platform_api_internal_api_key = SecretStr("internal")

    return S()


@pytest.mark.asyncio
async def test_platform_docs_client_search_uses_internal_endpoint() -> None:
    captured: dict[str, Any] = {}

    async def post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "matches": [
                    {
                        "title": "Installation",
                        "url": "/docs/installation",
                        "section": "Installation",
                        "heading": "Quick Start",
                        "snippet": "Install IncidentFlow MCP.",
                        "score": 0.93,
                        "source_path": "content/docs/installation.mdx",
                    }
                ]
            },
        )

    with patch("incidentflow_mcp.tools.docs_tools.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post.side_effect = post
        client_cls.return_value.__aenter__.return_value = client

        result = await PlatformAPIDocsClient(_settings()).search(query="install mcp", limit=3)

    assert captured["url"] == "http://platform-api:8000/internal/docs/search"
    assert captured["json"] == {"query": "install mcp", "limit": 3}
    assert captured["headers"]["X-Internal-Api-Key"] == "internal"
    assert result["matches"][0]["title"] == "Installation"


@pytest.mark.asyncio
async def test_incidentflow_docs_search_wraps_matches() -> None:
    with patch.object(
        PlatformAPIDocsClient,
        "search",
        AsyncMock(return_value={"matches": [{"title": "MCP", "url": "/docs/mcp"}]}),
    ):
        result = await incidentflow_docs_search(_settings(), query="mcp")

    assert result == {
        "query": "mcp",
        "total_matches": 1,
        "matches": [{"title": "MCP", "url": "/docs/mcp"}],
    }
