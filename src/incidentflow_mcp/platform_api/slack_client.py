from __future__ import annotations

import logging
from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import inject_trace_headers
from incidentflow_mcp.slack.slack_client import SlackThreadFetchResult, normalize_channel_name

logger = logging.getLogger(__name__)


class PlatformSlackAPIError(RuntimeError):
    """Structured platform-api Slack error exposed to MCP tool callers."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code


class PlatformSlackClient:
    """Slack read client backed by platform-api allowed-channel endpoints."""

    def __init__(self, settings: Settings, *, workspace_id: str) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for Slack platform mode")
        token = settings.platform_api_internal_api_key
        if token is None:
            raise ValueError("PLATFORM_API_INTERNAL_TOKEN is required for Slack platform mode")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds
        self._workspace_id = workspace_id
        self._headers = {
            "X-Internal-Api-Key": token.get_secret_value(),
            "X-MCP-Client-Id": "incidentflow-mcp",
        }

    def _raise_for_platform_error(self, response: httpx.Response) -> None:
        if not response.is_error:
            return
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
        if isinstance(payload, dict):
            code = payload.get("code")
            if isinstance(code, str) and code:
                message = payload.get("message")
                raise PlatformSlackAPIError(
                    code,
                    str(message) if message is not None else None,
                )
        response.raise_for_status()

    def _outbound_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        inject_trace_headers(headers)
        return headers

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._outbound_headers(),
            )
        self._raise_for_platform_error(response)
        return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._outbound_headers(),
            )
        self._raise_for_platform_error(response)
        return response.json()

    async def allowed_channels(self, *, purpose: str | None = None) -> list[dict[str, Any]]:
        params = {"workspace_id": self._workspace_id}
        if purpose:
            params["purpose"] = purpose
        payload = await self._get(
            "/internal/integrations/slack/allowed-channels",
            params,
        )
        return list(payload.get("channels", []) or [])

    async def resolve_channel(self, channel: str) -> tuple[str, str | None]:
        normalized = normalize_channel_name(channel)
        channels = await self.allowed_channels(purpose="alerts")
        if not channels:
            raise RuntimeError("no_enabled_alert_channel_for_workspace")
        for item in channels:
            channel_id = str(item.get("id") or "")
            name = str(item.get("name") or "")
            if normalized in {channel_id, name}:
                return channel_id, name
        raise RuntimeError(f"slack_channel_not_in_allowlist:{channel}")

    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._post(
            "/internal/integrations/slack/conversations-history",
            {"workspace_id": self._workspace_id, "channel_id": channel_id, "limit": limit},
        )
        return list(payload.get("messages", []) or [])

    async def permalink(self, *, channel_id: str, message_ts: str) -> str | None:
        payload = await self._post(
            "/internal/integrations/slack/permalink",
            {
                "workspace_id": self._workspace_id,
                "channel_id": channel_id,
                "message_ts": message_ts,
            },
        )
        permalink = payload.get("permalink")
        return str(permalink) if permalink else None

    async def resolve_user(self, user_id: str) -> str | None:
        _ = user_id
        return None

    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        payload = await self._post(
            "/internal/integrations/slack/conversations-replies",
            {
                "workspace_id": self._workspace_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "limit": max_replies + 1,
            },
        )
        messages = list(payload.get("messages", []) or [])
        root: dict[str, Any] | None = None
        replies: list[dict[str, Any]] = []
        for message in messages:
            if str(message.get("ts") or "") == thread_ts and root is None:
                root = message
                if include_root:
                    replies.append(message)
                continue
            replies.append(message)
        return SlackThreadFetchResult(
            root=root,
            replies=replies[:max_replies],
            messages=messages,
        )
