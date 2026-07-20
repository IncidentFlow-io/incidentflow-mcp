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
    extract_commands,
    summarize_thread_for_sre,
)

ThreadMode = Literal["none", "metadata", "full"]

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _redact_ips(text: str) -> str:
    """Mask IPv4 addresses so compact responses do not leak pod/node IPs."""
    return _IPV4_RE.sub("[redacted-ip]", text)


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
    alert_id: str | None = None
    name: str | None = None
    display_name: str | None = None
    alertmanager_url: str | None = None
    fired_at: str | None = None
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
    fingerprint: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    occurrences: int = 1
    deduplicated: bool = False
    monitoring_job: str | None = None
    workload: str | None = None
    business_service: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    summary: str
    raw_text: str | None = None
    # Commands (kubectl/helm/curl/...) extracted from the message regardless of
    # raw_text inclusion, so raw mode adds evidence without removing next steps.
    extracted_commands: list[str] = Field(default_factory=list)
    slack: SlackAlertContext | None = None
    thread: SlackThreadContext | None = None


class SlackAlertsOutput(BaseModel):
    channel_id: str
    channel_name: str | None = None
    requested_limit: int
    returned: int
    parsed: int = 0
    deduplicated: bool = True
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


def _clean_field(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"^[\s*•`_-]+", "", value.strip())
    cleaned = re.sub(r"[\s*`]+$", "", cleaned)
    return cleaned or None


def _is_system_message(message: dict[str, Any]) -> bool:
    subtype = str(message.get("subtype") or "").strip().lower()
    if subtype in {"channel_join", "channel_leave", "bot_add", "bot_remove"}:
        return True
    text = str(message.get("text") or "").strip().lower()
    return " has joined the channel" in text or " has left the channel" in text


def _infer_severity(text: str) -> str | None:
    explicit = _first_match(
        [r"Description:\s*`?([a-z]+)`?\s*-", r"\bseverity:\s*([^\n]+)"],
        text,
    )
    explicit = _clean_field(explicit)
    if explicit:
        normalized = explicit.lower()
        if normalized in {"critical", "high", "medium", "low", "info"}:
            return normalized
    haystack = text.lower()
    for severity in ("critical", "high", "medium", "low", "info"):
        if re.search(rf"\b{severity}\b", haystack):
            return severity
    return None


def _extract_alertmanager_url(text: str) -> str | None:
    match = re.search(r"<(https?://[^>|]+)(?:\|[^>]+)?>", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"https?://\S*alert\S*", text, flags=re.IGNORECASE)
    return match.group(0).rstrip(">)].,") if match else None


def _clean_alert_title(value: str | None) -> tuple[str | None, str | None, str | None]:
    cleaned = _clean_field(value)
    if not cleaned:
        return None, None, None

    alertmanager_url = _extract_alertmanager_url(cleaned)
    without_url = re.sub(r"<https?://[^>]+>", "", cleaned)
    without_url = re.sub(r"https?://\S+", "", without_url)
    without_url = re.sub(r"\s*[|-]\s*$", "", without_url.strip())
    display_name = _clean_field(without_url)
    if not display_name:
        return None, None, alertmanager_url

    canonical = display_name.split()[0]
    return canonical, display_name, alertmanager_url


def _thread_ts(message: dict[str, Any]) -> str:
    return str(message.get("thread_ts") or message.get("ts") or "")


def _message_participants(message: dict[str, Any]) -> list[str]:
    participants = message.get("reply_users")
    if isinstance(participants, list):
        return sorted({str(item) for item in participants if item})
    user = message.get("user") or message.get("bot_id")
    return [str(user)] if user else []


def _alert_datetime(alert: SlackAlertMessage) -> str | None:
    return alert.datetime_utc or alert.fired_at


def _alert_fingerprint(alert: SlackAlertMessage) -> str:
    parts = [
        alert.name or alert.alert_name or "unknown-alert",
        alert.cluster or "",
        alert.namespace or "",
        alert.workload or alert.pod or "",
        alert.monitoring_job or "",
    ]
    if not any(parts[1:]):
        parts.append(alert.summary[:80])
    return "|".join(str(part).strip().lower() for part in parts if str(part).strip())


def _merge_duplicate_alerts(alerts: list[SlackAlertMessage]) -> list[SlackAlertMessage]:
    merged: dict[str, SlackAlertMessage] = {}
    order: list[str] = []
    for alert in alerts:
        fingerprint = alert.fingerprint or _alert_fingerprint(alert)
        alert.fingerprint = fingerprint
        seen_at = _alert_datetime(alert)
        if fingerprint not in merged:
            alert.first_seen = seen_at
            alert.last_seen = seen_at
            merged[fingerprint] = alert
            order.append(fingerprint)
            continue

        current = merged[fingerprint]
        current.occurrences += 1
        current.deduplicated = True
        current.alert_count = max(
            value for value in [current.alert_count or 0, alert.alert_count or 0] if value is not None
        ) or None
        if seen_at:
            current.first_seen = min(
                value for value in [current.first_seen, seen_at] if value is not None
            )
            current.last_seen = max(
                value for value in [current.last_seen, seen_at] if value is not None
            )
        if alert.status == "firing" or current.status is None:
            current.status = alert.status
        elif alert.status == "resolved" and current.status != "firing":
            current.status = alert.status
    return [merged[fingerprint] for fingerprint in order]


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
    alert_name, display_name, alertmanager_url = _clean_alert_title(alert_name)
    if status_match is None and alert_name is None:
        return None

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

    service = _clean_field(_first_match([r"^Service:\s*(.+)$", r"\bjob:\s*([^\n]+)"], text))
    if not service and display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            service = _clean_field(parts[1])
    monitoring_job = service if service and re.search(r"\b(kubernetes|prometheus|scrape|pods)\b", service) else None
    cluster = _clean_field(_first_match([r"Cluster:\s*([^,\n]+)", r"\bcluster:\s*([^\n]+)"], text))
    namespace = _clean_field(
        _first_match([r"Namespace:\s*([^,\n]+)", r"\bnamespace:\s*([^\n]+)"], text)
    )
    pod = _clean_field(_first_match([r"^Pod:\s*(.+)$", r"\bpod:\s*([^\n]+)"], text))
    workload = _clean_field(
        _first_match([r"^Workload:\s*(.+)$", r"\bdeployment:\s*([^\n]+)", r"\bworkload:\s*([^\n]+)"], text)
    )
    severity = _infer_severity(text)
    labels = {
        key: value
        for key, value in {
            "cluster": cluster,
            "namespace": namespace,
            "pod": pod,
        }.items()
        if value
    }

    # Compact mode (default): drop raw_text and redact IPs so the response stays
    # small and safe for LLM safety layers. Commands stay available in all modes.
    extracted_commands = extract_commands(text)
    if not include_raw:
        summary = _redact_ips(summary)

    alert = SlackAlertMessage(
        alert_id=f"slack-{ts}" if ts else None,
        name=alert_name,
        display_name=display_name,
        alertmanager_url=alertmanager_url,
        fired_at=datetime_utc,
        ts=ts,
        datetime_utc=datetime_utc,
        channel_id=channel_id,
        channel_name=channel_name,
        permalink=permalink,
        status=status,
        alert_count=alert_count,
        alert_name=alert_name,
        service=service,
        cluster=cluster,
        namespace=namespace,
        pod=pod,
        severity=severity,
        monitoring_job=monitoring_job,
        workload=workload,
        business_service=None if monitoring_job == service else service,
        labels=labels,
        summary=summary,
        raw_text=text if include_raw else None,
        extracted_commands=extracted_commands,
        slack=SlackAlertContext(
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=ts,
            thread_ts=thread_ts,
            permalink=permalink,
            thread_permalink=thread_permalink or permalink,
        ),
    )
    alert.fingerprint = _alert_fingerprint(alert)
    alert.first_seen = datetime_utc
    alert.last_seen = datetime_utc
    return alert


async def fetch_slack_alerts(
    *,
    token: str | None = None,
    channel: str,
    limit: int,
    include_raw: bool = False,
    include_threads: bool = False,
    thread_mode: ThreadMode = "none",
    max_thread_replies: int = 20,
    include_system_messages: bool = False,
    deduplicate: bool = True,
    client: Any | None = None,
) -> SlackAlertsOutput:
    if client is None:
        if token is None:
            raise ValueError("Slack token or platform client is required")
        client = SlackClient(token)
    cache: dict[tuple[str, str], SlackThreadContext] = {}
    channel_id, channel_name = await client.resolve_channel(channel)
    messages = await client.conversation_history(channel_id=channel_id, limit=limit)

    selected_mode = thread_mode if include_threads else "none"
    alerts: list[SlackAlertMessage] = []
    for message in messages:
        if not include_system_messages and _is_system_message(message):
            continue
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

    parsed_count = len(alerts)
    if deduplicate:
        alerts = _merge_duplicate_alerts(alerts)

    return SlackAlertsOutput(
        channel_id=channel_id,
        channel_name=channel_name,
        requested_limit=limit,
        returned=len(alerts),
        parsed=parsed_count,
        deduplicated=deduplicate,
        alerts=alerts,
    )


async def fetch_slack_alert_thread(
    *,
    token: str | None = None,
    channel_id: str,
    message_ts: str,
    include_root: bool = True,
    include_raw: bool = False,
    max_replies: int = 50,
    client: Any | None = None,
) -> SlackAlertThreadOutput:
    if client is None:
        if token is None:
            raise ValueError("Slack token or platform client is required")
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
            include_raw=include_raw,
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
    token: str | None = None,
    channel_id: str,
    thread_ts: str,
    alert_context: dict[str, Any] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    output = await fetch_slack_alert_thread(
        token=token,
        channel_id=channel_id,
        message_ts=thread_ts,
        include_root=False,
        max_replies=50,
        client=client,
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
