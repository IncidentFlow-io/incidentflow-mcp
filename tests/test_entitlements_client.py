from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from incidentflow_mcp.platform_api.entitlements_client import PlatformEntitlementsClient


@pytest.mark.asyncio
async def test_entitlement_client_caps_cache_ttl_at_sixty_seconds() -> None:
    settings = MagicMock()
    settings.platform_api_base_url = "http://platform-api:8000"
    settings.platform_api_timeout_seconds = 5.0
    settings.platform_api_internal_api_key.get_secret_value.return_value = "key"
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "workspace_id": "workspace-1", "access_state": "team", "limits": {},
        "features": {}, "usage": {}, "upgrade_url": "https://app.test/settings?section=billing",
        "ttl_seconds": 999,
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        snapshot = await PlatformEntitlementsClient(settings).get("workspace-1")
    assert snapshot.ttl_seconds == 60
    assert snapshot.writable is True
