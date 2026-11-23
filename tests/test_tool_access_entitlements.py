"""Entitlement gates distinguish read-only tools from mutations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from incidentflow_mcp.mcp.access import ToolAccessResolver
from incidentflow_mcp.platform_api.entitlements_client import (
    EntitlementSnapshot,
    PlatformEntitlementsClient,
)
from incidentflow_mcp.tools.registry import get_tool_specs


def _snapshot(*, readable: bool, writable: bool) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        workspace_id="workspace-1",
        access_state="remediation",
        readable=readable,
        writable=writable,
        limits={},
        features={"feature.read_only_mcp": readable},
        usage={},
        upgrade_url="https://app.test/settings?section=billing",
        ttl_seconds=60,
    )


def _resolver(integration_resolver: AsyncMock) -> ToolAccessResolver:
    specs = {spec.name: spec for spec in get_tool_specs()}
    request_context = MagicMock()
    request_context.workspace_id.return_value = "workspace-1"
    request_context.principal.return_value = MagicMock()
    return ToolAccessResolver(
        settings=MagicMock(),
        request_context=request_context,
        specs=specs,
        integration_context_resolver=integration_resolver,
    )


@pytest.mark.asyncio
async def test_logs_list_sources_allows_read_only_access_when_writes_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        PlatformEntitlementsClient,
        "get",
        AsyncMock(return_value=_snapshot(readable=True, writable=False)),
    )

    result = await _resolver(integration_resolver).resolve("logs_list_sources")

    assert result is None
    integration_resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_tool_stays_blocked_when_writes_are_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        PlatformEntitlementsClient,
        "get",
        AsyncMock(return_value=_snapshot(readable=True, writable=False)),
    )

    result = await _resolver(integration_resolver).resolve("knowledge_upsert")

    assert result == (
        '{"code":"BILLING_REMEDIATION_REQUIRED",'
        '"message":"Billing needs attention before this workspace action can run"}'
    )
    integration_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_logs_return_entitlement_error_when_read_access_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        PlatformEntitlementsClient,
        "get",
        AsyncMock(return_value=_snapshot(readable=False, writable=False)),
    )

    result = await _resolver(integration_resolver).resolve("logs_list_sources")

    assert result == (
        '{"code":"BILLING_REMEDIATION_REQUIRED",'
        '"message":"Read access is not available for this workspace"}'
    )
    integration_resolver.assert_not_awaited()
