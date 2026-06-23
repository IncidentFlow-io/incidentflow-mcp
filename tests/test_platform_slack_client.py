import httpx
import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.platform_api.slack_client import PlatformSlackAPIError, PlatformSlackClient


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        platform_api_base_url="https://platform.example",
        platform_api_internal_api_key="internal-token",
        environment="test",
        log_level="warning",
    )


def _client() -> PlatformSlackClient:
    return PlatformSlackClient(_settings(), workspace_id="workspace-1")


def test_platform_slack_error_preserves_platform_code() -> None:
    client = _client()
    response = httpx.Response(
        404,
        json={
            "code": "slack_not_connected_for_workspace",
            "message": "Slack integration is not connected",
        },
        request=httpx.Request("GET", "https://platform.example/internal/integrations/slack"),
    )

    with pytest.raises(PlatformSlackAPIError) as exc_info:
        client._raise_for_platform_error(response)

    assert exc_info.value.code == "slack_not_connected_for_workspace"
    assert str(exc_info.value) == "slack_not_connected_for_workspace"


@pytest.mark.asyncio
async def test_resolve_channel_requires_enabled_alert_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    async def allowed_channels(*, purpose: str | None = None) -> list[dict[str, object]]:
        assert purpose == "alerts"
        return []

    monkeypatch.setattr(client, "allowed_channels", allowed_channels)

    with pytest.raises(RuntimeError, match="no_enabled_alert_channel_for_workspace"):
        await client.resolve_channel("alerts")


@pytest.mark.asyncio
async def test_resolve_channel_rejects_channel_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    async def allowed_channels(*, purpose: str | None = None) -> list[dict[str, object]]:
        assert purpose == "alerts"
        return [{"id": "C_ALLOWED", "name": "alerts"}]

    monkeypatch.setattr(client, "allowed_channels", allowed_channels)

    with pytest.raises(RuntimeError, match="slack_channel_not_in_allowlist:ops"):
        await client.resolve_channel("ops")


@pytest.mark.asyncio
async def test_resolve_channel_accepts_allowed_channel_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    async def allowed_channels(*, purpose: str | None = None) -> list[dict[str, object]]:
        assert purpose == "alerts"
        return [{"id": "C_ALLOWED", "name": "alerts"}]

    monkeypatch.setattr(client, "allowed_channels", allowed_channels)

    assert await client.resolve_channel("#alerts") == ("C_ALLOWED", "alerts")
