from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from incidentflow_mcp.slack.slack_client import SlackClient, SlackThreadFetchResult
from incidentflow_mcp.slack.thread_analyzer import (
    ThreadAggregateAnalysis,
    ThreadReplyAnalysis,
    analyze_replies,
    analyze_reply,
    summarize_thread_for_sre,
)

ThreadMode = Literal["none", "metadata", "full"]


class SlackAlertContext(BaseModel):
    channel_id: str
    channel_name: str | None = None
    message_ts: str
    thread_ts: str
    permalink: str | None = None
    thread_permalink: str | None = None


class SlackThreadContext(BaseModel):
    reply_count: int = 0
    last_reply_ts: str | None = None
    participants: list[str] = Field(default_factory=list)
    replies: list[ThreadReplyAnalysis] = Field(default_factory=list)
    analysis: ThreadAggregateAnalysis | None = None
    warning: str | None = None


class SlackAlertMessage(BaseModel):
    ts: str
    datetime_utc: str | None = None
    channel_id: str
    channel_name: str | None = None
    permalink: str | None = None
    status: str | None = None
    alert_count: int | None = None
    alert_name: str | None = None
    service: str | None = None
    cluster: str | None = None
    namespace: str | None = None
    pod: str | None = None
    severity: str | None = None
    summary: str
    raw_text: str | None = None
    slack: SlackAlertContext | None = None
    thread: SlackThreadContext | None = None


class SlackAlertsOutput(BaseModel):
    channel_id: str
    channel_name: str | None = None
    requested_limit: int
    returned: int
    alerts: list[SlackAlertMessage] = Field(default_factory=list)


class SlackAlertThreadOutput(BaseModel):
    root_alert: SlackAlertMessage | None = None
    thread: SlackThreadContext
    analysis: ThreadAggregateAnalysis


def _message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    text = str(message.get("text") or "").strip()
    if text:
        parts.append(text)

    for attachment in message.get("attachments", []) or []:
        title = str(attachment.get("title") or "").strip()
        fallback = str(attachment.get("fallback") or "").strip()
        body = str(attachment.get("text") or "").strip()
        if title:
            parts.append(title)
        if body:
            parts.append(body)
        if fallback and fallback not in parts:
            parts.append(fallback)
        for field in attachment.get("fields", []) or []:
            field_title = str(field.get("title") or "").strip()
            field_value = str(field.get("value") or "").strip()
            if field_title or field_value:
                parts.append(f"{field_title}: {field_value}".strip(": "))

    for block in message.get("blocks", []) or []:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict) and text_obj.get("text"):
            parts.append(str(text_obj["text"]))
        for field in block.get("fields", []) or []:
            if isinstance(field, dict) and field.get("text"):
                parts.append(str(field["text"]))

    return "\n".join(part for part in parts if part).strip()


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _thread_ts(message: dict[str, Any]) -> str:
    return str(message.get("thread_ts") or message.get("ts") or "")


def _message_participants(message: dict[str, Any]) -> list[str]:
    participants = message.get("reply_users")
    if isinstance(participants, list):
        return sorted({str(item) for item in participants if item})
    user = message.get("user") or message.get("bot_id")
    return [str(user)] if user else []


async def _reply_username(
    *,
    client: SlackClient,
    reply: dict[str, Any],
    user_cache: dict[str, str | None],
) -> str | None:
    direct_username = str(reply.get("username") or "").strip()
    if direct_username:
        return direct_username

    bot_profile = reply.get("bot_profile")
    if isinstance(bot_profile, dict):
        bot_name = str(bot_profile.get("name") or "").strip()
        if bot_name:
            return bot_name

    user_id = str(reply.get("user") or "").strip()
    if user_id:
        if user_id not in user_cache:
            user_cache[user_id] = await client.resolve_user(user_id)
        return user_cache[user_id]

    bot_id = str(reply.get("bot_id") or "").strip()
    return bot_id or None


def _thread_metadata_from_message(message: dict[str, Any]) -> SlackThreadContext:
    reply_count = int(message.get("reply_count") or 0)
    return SlackThreadContext(
        reply_count=reply_count,
        last_reply_ts=str(message.get("latest_reply") or "") or None,
        participants=_message_participants(message),
    )


def _parse_alert_message(
    *,
    message: dict[str, Any],
    channel_id: str,
    channel_name: str | None,
    permalink: str | None,
    include_raw: bool,
    thread_permalink: str | None = None,
) -> SlackAlertMessage | None:
    text = _message_text(message)
    if not text:
        return None

    status_match = re.search(r"\[(FIRING|RESOLVED|PENDING)(?::(\d+))?\]", text, re.IGNORECASE)
    status = status_match.group(1).lower() if status_match else None
    alert_count = int(status_match.group(2)) if status_match and status_match.group(2) else None

    alert_name = None
    if status_match:
        title_line = text[status_match.start() :].splitlines()[0]
        title_line = re.sub(
            r"^\[(?:FIRING|RESOLVED|PENDING)(?::\d+)?\]\s*",
            "",
            title_line,
            flags=re.I,
        )
        alert_name = title_line.strip(" *") or None
    alert_name = alert_name or _first_match([r"^Alert:\s*(.+)$", r"alertname\s*=\s*([^\n]+)"], text)

    summary = _first_match(
        [
            r"^Description:\s*(.+)$",
            r"^Annotations:\s*\n\s*-\s*summary\s*=\s*(.+)$",
            r"^summary\s*[:=]\s*(.+)$",
            r"^\*\*?(Firing|Resolved|Pending)\*\*?$",
        ],
        text,
    )
    if not summary:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        summary = lines[0][:300] if lines else ""

    ts = str(message.get("ts") or "")
    thread_ts = _thread_ts(message)
    datetime_utc = None
    try:
        datetime_utc = datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (TypeError, ValueError):
        pass

    return SlackAlertMessage(
        ts=ts,
        datetime_utc=datetime_utc,
        channel_id=channel_id,
        channel_name=channel_name,
        permalink=permalink,
        status=status,
        alert_count=alert_count,
        alert_name=alert_name,
        service=_first_match([r"^Service:\s*(.+)$", r"\bjob:\s*([^\n]+)"], text),
        cluster=_first_match([r"Cluster:\s*([^,\n]+)", r"\bcluster:\s*([^\n]+)"], text),
        namespace=_first_match([r"Namespace:\s*([^,\n]+)", r"\bnamespace:\s*([^\n]+)"], text),
        pod=_first_match([r"^Pod:\s*(.+)$", r"\bpod:\s*([^\n]+)"], text),
        severity=_first_match(
            [r"Description:\s*`?([a-z]+)`?\s*-", r"\bseverity:\s*([^\n]+)"],
            text,
        ),
        summary=summary,
        raw_text=text if include_raw else None,
        slack=SlackAlertContext(
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=ts,
            thread_ts=thread_ts,
            permalink=permalink,
            thread_permalink=thread_permalink or permalink,
        ),
    )


async def fetch_slack_alerts(
    *,
    token: str,
    channel: str,
    limit: int,
    include_raw: bool = False,
    include_threads: bool = False,
    thread_mode: ThreadMode = "none",
    max_thread_replies: int = 20,
) -> SlackAlertsOutput:
    client = SlackClient(token)
    cache: dict[tuple[str, str], SlackThreadContext] = {}
    channel_id, channel_name = await client.resolve_channel(channel)
    messages = await client.conversation_history(channel_id=channel_id, limit=limit)

    selected_mode = thread_mode if include_threads else "none"
    alerts: list[SlackAlertMessage] = []
    for message in messages:
        ts = str(message.get("ts") or "")
        permalink = await client.permalink(channel_id=channel_id, message_ts=ts) if ts else None
        thread_ts = _thread_ts(message)
        thread_permalink = (
            await client.permalink(channel_id=channel_id, message_ts=thread_ts)
            if thread_ts and thread_ts != ts
            else permalink
        )

        parsed = _parse_alert_message(
            message=message,
            channel_id=channel_id,
            channel_name=channel_name,
            permalink=permalink,
            include_raw=include_raw,
            thread_permalink=thread_permalink,
        )
        if parsed is None:
            continue

        if selected_mode == "metadata":
            parsed.thread = _thread_metadata_from_message(message)
        elif selected_mode == "full":
            parsed.thread = await _fetch_thread_context(
                client=client,
                cache=cache,
                channel_id=channel_id,
                thread_ts=thread_ts,
                max_replies=max_thread_replies,
                base_message=message,
            )

        alerts.append(parsed)

    return SlackAlertsOutput(
        channel_id=channel_id,
        channel_name=channel_name,
        requested_limit=limit,
        returned=len(alerts),
        alerts=alerts,
    )


async def fetch_slack_alert_thread(
    *,
    token: str,
    channel_id: str,
    message_ts: str,
    include_root: bool = True,
    max_replies: int = 50,
) -> SlackAlertThreadOutput:
    client = SlackClient(token)
    fetched = await client.thread_replies(
        channel_id=channel_id,
        thread_ts=message_ts,
        max_replies=max_replies,
        include_root=False,
    )
    root_message = fetched.root
    if root_message is None and fetched.messages:
        root_message = fetched.messages[0]

    root_permalink = await client.permalink(channel_id=channel_id, message_ts=message_ts)
    root_alert = (
        _parse_alert_message(
            message=root_message,
            channel_id=channel_id,
            channel_name=None,
            permalink=root_permalink,
            include_raw=include_root,
            thread_permalink=root_permalink,
        )
        if root_message is not None and include_root
        else None
    )

    thread = await _thread_context_from_fetch(
        client=client,
        channel_id=channel_id,
        fetched=fetched,
        base_message=root_message or {"ts": message_ts},
    )
    return SlackAlertThreadOutput(
        root_alert=root_alert,
        thread=thread,
        analysis=thread.analysis or analyze_replies(thread.replies),
    )


async def summarize_incident_thread(
    *,
    token: str,
    channel_id: str,
    thread_ts: str,
    alert_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = await fetch_slack_alert_thread(
        token=token,
        channel_id=channel_id,
        message_ts=thread_ts,
        include_root=False,
        max_replies=50,
    )
    return summarize_thread_for_sre(
        replies=output.thread.replies,
        alert_context=alert_context,
    )


async def _fetch_thread_context(
    *,
    client: SlackClient,
    cache: dict[tuple[str, str], SlackThreadContext],
    channel_id: str,
    thread_ts: str,
    max_replies: int,
    base_message: dict[str, Any],
) -> SlackThreadContext:
    key = (channel_id, thread_ts)
    if key in cache:
        return cache[key]

    fetched = await client.thread_replies(
        channel_id=channel_id,
        thread_ts=thread_ts,
        max_replies=max_replies,
        include_root=False,
    )
    thread = await _thread_context_from_fetch(
        client=client,
        channel_id=channel_id,
        fetched=fetched,
        base_message=base_message,
    )
    cache[key] = thread
    return thread


async def _thread_context_from_fetch(
    *,
    client: SlackClient,
    channel_id: str,
    fetched: SlackThreadFetchResult,
    base_message: dict[str, Any],
) -> SlackThreadContext:
    reply_analyses: list[ThreadReplyAnalysis] = []
    participants: set[str] = set(_message_participants(base_message))
    user_cache: dict[str, str | None] = {}
    last_reply_ts = str(base_message.get("latest_reply") or "") or None

    for reply in fetched.replies:
        ts = str(reply.get("ts") or "")
        user = reply.get("user") or reply.get("bot_id")
        if user:
            participants.add(str(user))
        if ts:
            last_reply_ts = ts
        permalink = await client.permalink(channel_id=channel_id, message_ts=ts) if ts else None
        reply_analyses.append(
            analyze_reply(
                text=_message_text(reply) or str(reply.get("text") or ""),
                ts=ts,
                user=str(user) if user else None,
                username=await _reply_username(
                    client=client,
                    reply=reply,
                    user_cache=user_cache,
                ),
                permalink=permalink,
            )
        )

    analysis = analyze_replies(reply_analyses)
    reply_count = int(base_message.get("reply_count") or len(reply_analyses))
    return SlackThreadContext(
        reply_count=reply_count,
        last_reply_ts=last_reply_ts,
        participants=sorted(participants),
        replies=reply_analyses,
        analysis=analysis,
        warning=fetched.warning,
    )
