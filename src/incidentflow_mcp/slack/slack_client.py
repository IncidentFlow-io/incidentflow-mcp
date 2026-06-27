from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")


class SlackAPIError(RuntimeError):
    """Raised when Slack returns a non-OK API response."""

    def __init__(
        self,
        *,
        method: str,
        error: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(f"slack_api_error:{method}:{error}")
        self.method = method
        self.error = error
        self.retry_after_seconds = retry_after_seconds


@dataclass
class SlackThreadFetchResult:
    root: dict[str, Any] | None
    replies: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    warning: str | None = None


class SlackClient:
    """Small async Slack Web API client for read-only MCP tools."""

    def __init__(self, token: str, *, timeout_seconds: float = 10.0) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    async def api_get(
        self,
        method: str,
        params: dict[str, Any],
        *,
        retry_on_rate_limit: bool = True,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(
                f"https://slack.com/api/{method}",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )

        retry_after = _retry_after_seconds(response)
        if response.status_code == 429:
            if retry_on_rate_limit and retry_after is not None:
                await asyncio.sleep(min(retry_after, 5))
                return await self.api_get(method, params, retry_on_rate_limit=False)
            raise SlackAPIError(
                method=method,
                error="ratelimited",
                retry_after_seconds=retry_after,
            )

        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            error = str(data.get("error", "unknown_error"))
            retry_after = retry_after or _retry_after_seconds_from_data(data)
            if error == "ratelimited" and retry_on_rate_limit and retry_after is not None:
                await asyncio.sleep(min(retry_after, 5))
                return await self.api_get(method, params, retry_on_rate_limit=False)
            raise SlackAPIError(
                method=method,
                error=error,
                retry_after_seconds=retry_after,
            )
        return data

    async def resolve_channel(self, channel: str) -> tuple[str, str | None]:
        normalized = normalize_channel_name(channel)
        if normalized.startswith(("C", "G", "D")) and len(normalized) >= 8:
            info = await self.api_get("conversations.info", {"channel": normalized})
            channel_obj = info.get("channel", {})
            return normalized, channel_obj.get("name")

        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "types": "public_channel,private_channel",
                "limit": 200,
                "exclude_archived": "true",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self.api_get("conversations.list", params)
            for item in data.get("channels", []):
                if item.get("name") == normalized:
                    return str(item["id"]), str(item.get("name") or normalized)
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        raise RuntimeError(f"slack_channel_not_found:{channel}")

    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        data = await self.api_get(
            "conversations.history",
            {"channel": channel_id, "limit": limit, "inclusive": "true"},
        )
        return list(data.get("messages", []) or [])

    async def permalink(self, *, channel_id: str, message_ts: str) -> str | None:
        try:
            data = await self.api_get(
                "chat.getPermalink",
                {"channel": channel_id, "message_ts": message_ts},
            )
            permalink = data.get("permalink")
            return str(permalink) if permalink else None
        except Exception:
            return None

    async def resolve_user(self, user_id: str) -> str | None:
        try:
            data = await self.api_get("users.info", {"user": user_id})
        except SlackAPIError as exc:
            logger.info(
                "slack_user_resolution_failed user_id=%s error=%s retry_after_seconds=%s",
                user_id,
                exc.error,
                exc.retry_after_seconds,
            )
            return None
        except Exception as exc:
            logger.info(
                "slack_user_resolution_failed user_id=%s error=%s",
                user_id,
                exc.__class__.__name__,
            )
            return None

        user = data.get("user")
        if not isinstance(user, dict):
            logger.info(
                "slack_user_resolution_empty user_id=%s reason=missing_user_object",
                user_id,
            )
            return None

        profile = user.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        for value in (
            profile.get("display_name"),
            profile.get("display_name_normalized"),
            profile.get("real_name"),
            profile.get("real_name_normalized"),
            user.get("real_name"),
            user.get("name"),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        logger.info("slack_user_resolution_empty user_id=%s reason=missing_display_name", user_id)
        return None

    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        if not _SLACK_TS_RE.match(thread_ts):
            return SlackThreadFetchResult(
                root=None, replies=[], messages=[], warning="invalid_thread_ts"
            )

        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        remaining = max(0, max_replies) + 1
        warning: str | None = None

        try:
            while remaining > 0:
                params: dict[str, Any] = {
                    "channel": channel_id,
                    "ts": thread_ts,
                    "limit": min(max(1, remaining), 200),
                    "inclusive": "true",
                }
                if cursor:
                    params["cursor"] = cursor
                data = await self.api_get("conversations.replies", params)
                batch = list(data.get("messages", []) or [])
                messages.extend(batch)
                remaining -= len(batch)
                cursor = str(data.get("response_metadata", {}).get("next_cursor") or "")
                if not cursor or not batch:
                    break
        except SlackAPIError as exc:
            warning = exc.error
        except Exception as exc:
            warning = exc.__class__.__name__

        root: dict[str, Any] | None = None
        replies: list[dict[str, Any]] = []
        for message in messages:
            ts = str(message.get("ts") or "")
            if ts == thread_ts and root is None:
                root = message
                if include_root:
                    replies.append(message)
                continue
            replies.append(message)

        if not include_root:
            replies = [m for m in replies if str(m.get("ts") or "") != thread_ts]
        if max_replies >= 0:
            replies = replies[:max_replies]

        return SlackThreadFetchResult(
            root=root,
            replies=replies,
            messages=messages,
            warning=warning,
        )


def normalize_channel_name(channel: str) -> str:
    return channel.removeprefix("#").strip()


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _retry_after_seconds_from_data(data: dict[str, Any]) -> int | None:
    value = data.get("retry_after") or data.get("retry_after_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
