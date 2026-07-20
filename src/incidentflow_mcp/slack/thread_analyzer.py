from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

LinkType = Literal[
    "grafana",
    "runbook",
    "logs",
    "kibana",
    "notion",
    "confluence",
    "github",
    "generic",
]
Confidence = Literal["low", "medium", "high"]


class NormalizedLink(BaseModel):
    url: str
    label: str | None = None
    type: LinkType = "generic"


class ThreadReplyAnalysis(BaseModel):
    ts: str
    user: str | None = None
    username: str | None = None
    text: str
    permalink: str | None = None
    contains_command: bool = False
    contains_runbook_link: bool = False
    contains_hypothesis: bool = False
    contains_resolution: bool = False
    commands: list[str] = Field(default_factory=list)
    links: list[NormalizedLink] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)


class ThreadAggregateAnalysis(BaseModel):
    summary: str
    engineer_hypotheses: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    commands_found: list[str] = Field(default_factory=list)
    runbook_links: list[NormalizedLink] = Field(default_factory=list)
    links: list[NormalizedLink] = Field(default_factory=list)
    mentioned_services: list[str] = Field(default_factory=list)
    possible_resolution: str | None = None
    resolution_signal: bool = False
    resolution_confidence: Confidence = "low"
    confidence: Confidence = "low"


_COMMAND_RE = re.compile(
    r"(?im)^\s*(?:`{1,3})?\s*((?:kubectl|helm|k\s+logs|curl|jq|aws|terraform)\b[^\n`]*)"
)
_SLACK_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_RAW_URL_RE = re.compile(r"(?<![\](<])\bhttps?://[^\s>)]+")
_HYPOTHESIS_RE = re.compile(
    r"(?i)\b(i think|looks like|probably|seems like|может быть|думаю|похоже|"
    r"скорее всего|ймовірно|схоже|мабуть)\b[^.\n]*"
)
_RESOLUTION_RE = re.compile(
    r"(?i)\b(fixed|resolved|done|mitigated|rolled back|rollback complete|"
    r"перезапустил|починил|починили|зафиксили|откатили|решено|виправив|"
    r"виправили|відкотили|вирішено)\b[^.\n]*"
)
_NEGATED_RESOLUTION_RE = re.compile(
    r"(?i)(?:"
    r"\bnot\b|\bnot\s+yet\b|\bnever\b|\bno\b|"
    r"не\b|нет\b|ещ[её]\s+не|пока\s+не|"
    r"ще\s+не|не\s+вважаю|не\s+считаю"
    r")"
)
_ACTION_RE = re.compile(
    r"(?i)\b(try|restart|rollback|roll back|scale|check|investigate|"
    r"перезапусти|проверь|откати|масштабируй|перевір|відкоти)\b[^.\n]*"
)
_SERVICE_HINT_RE = re.compile(
    r"(?i)\b(?:service|svc|namespace|ns|deployment|deploy|pod|job)[:=/\s]+([a-z0-9][a-z0-9_.-]{1,80})"
)


def analyze_reply(
    *,
    text: str,
    ts: str = "",
    user: str | None = None,
    username: str | None = None,
    permalink: str | None = None,
) -> ThreadReplyAnalysis:
    commands = extract_commands(text)
    links = extract_links(text)
    hypotheses = _matches(_HYPOTHESIS_RE, text)
    resolutions = extract_resolutions(text)
    return ThreadReplyAnalysis(
        ts=ts,
        user=user,
        username=username,
        text=text,
        permalink=permalink,
        contains_command=bool(commands),
        contains_runbook_link=any(link.type == "runbook" for link in links),
        contains_hypothesis=bool(hypotheses),
        contains_resolution=bool(resolutions),
        commands=commands,
        links=links,
        hypotheses=hypotheses,
        resolutions=resolutions,
    )


def analyze_replies(replies: list[ThreadReplyAnalysis]) -> ThreadAggregateAnalysis:
    hypotheses = _dedupe(item for reply in replies for item in reply.hypotheses)
    commands = _dedupe(item for reply in replies for item in reply.commands)
    links = _dedupe_links(link for reply in replies for link in reply.links)
    runbook_links = [link for link in links if link.type == "runbook"]
    resolutions = _dedupe(item for reply in replies for item in reply.resolutions)
    suggested_actions = _dedupe(
        action.strip()
        for reply in replies
        for action in _matches(_ACTION_RE, reply.text)
        if action.strip() not in commands
    )
    mentioned_services = _dedupe(
        match.group(1).strip("`*_")
        for reply in replies
        for match in _SERVICE_HINT_RE.finditer(reply.text)
    )

    resolution_signal = bool(resolutions)
    confidence: Confidence = "low"
    if hypotheses and (commands or links):
        confidence = "high"
    elif hypotheses or commands or links or resolutions:
        confidence = "medium"

    resolution_confidence: Confidence = "low"
    resolution_text = " ".join(resolutions).lower()
    strong_resolution_words = ("resolved", "fixed", "решено", "вирішено")
    if resolution_signal and any(word in resolution_text for word in strong_resolution_words):
        resolution_confidence = "medium"
    if resolution_signal and commands:
        resolution_confidence = "high"

    summary_parts: list[str] = []
    if hypotheses:
        summary_parts.append(f"{len(hypotheses)} hypothesis signal(s)")
    if commands:
        summary_parts.append(f"{len(commands)} command(s)")
    if runbook_links:
        summary_parts.append(f"{len(runbook_links)} runbook link(s)")
    if resolution_signal:
        summary_parts.append("resolution signal present")
    summary = (
        ", ".join(summary_parts) if summary_parts else "No actionable thread signals detected."
    )

    return ThreadAggregateAnalysis(
        summary=summary,
        engineer_hypotheses=hypotheses,
        suggested_actions=suggested_actions,
        commands_found=commands,
        runbook_links=runbook_links,
        links=links,
        mentioned_services=mentioned_services,
        possible_resolution=resolutions[-1] if resolutions else None,
        resolution_signal=resolution_signal,
        resolution_confidence=resolution_confidence,
        confidence=confidence,
    )


def extract_commands(text: str) -> list[str]:
    return _dedupe(match.group(1).strip() for match in _COMMAND_RE.finditer(text))


def extract_resolutions(text: str) -> list[str]:
    return _dedupe(
        match.group(0).strip()
        for match in _RESOLUTION_RE.finditer(text)
        if not _is_negated_resolution(text, match.start())
    )


def extract_links(text: str) -> list[NormalizedLink]:
    links: list[NormalizedLink] = []
    occupied: list[tuple[int, int]] = []

    for match in _SLACK_LINK_RE.finditer(text):
        links.append(_link(match.group(1), match.group(2)))
        occupied.append(match.span())

    for match in _MARKDOWN_LINK_RE.finditer(text):
        links.append(_link(match.group(2), match.group(1)))
        occupied.append(match.span())

    for match in _RAW_URL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        links.append(_link(match.group(0), None))

    return _dedupe_links(links)


def summarize_thread_for_sre(
    *,
    replies: list[ThreadReplyAnalysis],
    alert_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analysis = analyze_replies(replies)
    status = "unknown"
    if analysis.resolution_signal:
        status = (
            "mitigated" if analysis.resolution_confidence in {"medium", "high"} else "investigating"
        )
    elif analysis.engineer_hypotheses or analysis.commands_found:
        status = "investigating"

    title = "Slack thread context"
    if alert_context:
        title = str(
            alert_context.get("alert_name")
            or alert_context.get("name")
            or alert_context.get("summary")
            or title
        )[:160]

    risks, open_questions = _alert_context_risks(alert_context)
    return {
        "title": title,
        "status": status,
        "summary": analysis.summary,
        "what_engineers_said": [reply.text for reply in replies],
        "probable_root_cause": (
            analysis.engineer_hypotheses[-1] if analysis.engineer_hypotheses else None
        ),
        "actions_taken": analysis.commands_found,
        "next_actions": analysis.suggested_actions,
        "runbooks": [link.model_dump() for link in analysis.runbook_links],
        "commands": analysis.commands_found,
        "risks": risks,
        "open_questions": open_questions,
    }


def _alert_context_risks(alert_context: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if not isinstance(alert_context, dict):
        return [], []

    risks: list[str] = []
    open_questions: list[str] = []
    labels = alert_context.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    alert_cluster = str(alert_context.get("cluster") or labels.get("cluster") or "").strip()
    expected_cluster = str(
        alert_context.get("expected_cluster")
        or alert_context.get("target_cluster")
        or alert_context.get("current_cluster")
        or alert_context.get("environment")
        or ""
    ).strip()

    if expected_cluster and alert_cluster and alert_cluster != expected_cluster:
        risks.append(
            f"Slack evidence is from cluster {alert_cluster}, not {expected_cluster}."
        )
        open_questions.append(
            "Is this Slack thread relevant to the current incident or only historical context?"
        )

    observed_at = _alert_context_timestamp(alert_context)
    if observed_at is not None:
        age_seconds = (datetime.now(tz=UTC) - observed_at).total_seconds()
        if age_seconds > 7 * 24 * 60 * 60:
            age_days = int(age_seconds // (24 * 60 * 60))
            risks.append(f"Slack evidence is stale: approximately {age_days} days old.")
            if not open_questions:
                open_questions.append(
                    "Is this Slack thread still current, or only historical incident context?"
                )

    return risks, open_questions


def _alert_context_timestamp(alert_context: dict[str, Any]) -> datetime | None:
    for key in ("fired_at", "datetime_utc", "started_at", "ts"):
        value = alert_context.get(key)
        if value is None:
            continue
        if key == "ts":
            try:
                return datetime.fromtimestamp(float(value), tz=UTC)
            except (TypeError, ValueError, OSError):
                continue
        text = str(value).strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _link(url: str, label: str | None) -> NormalizedLink:
    return NormalizedLink(url=url, label=label, type=_classify_link(url, label))


def _classify_link(url: str, label: str | None) -> LinkType:
    haystack = f"{url} {label or ''}".lower()
    if "grafana" in haystack:
        return "grafana"
    if "kibana" in haystack or "logs" in haystack or "log" in haystack:
        return "logs" if "kibana" not in haystack else "kibana"
    if "runbook" in haystack or "playbook" in haystack:
        return "runbook"
    if "notion" in haystack:
        return "notion"
    if "confluence" in haystack or "atlassian" in haystack:
        return "confluence"
    if "github.com" in haystack:
        return "github"
    return "generic"


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return _dedupe(match.group(0).strip() for match in pattern.finditer(text))


def _is_negated_resolution(text: str, match_start: int) -> bool:
    line_start = text.rfind("\n", 0, match_start) + 1
    prefix = text[line_start:match_start].lower()
    nearby_prefix = prefix[-48:]
    return bool(_NEGATED_RESOLUTION_RE.search(nearby_prefix))


def _dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = str(item).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _dedupe_links(items: Any) -> list[NormalizedLink]:
    seen: set[str] = set()
    result: list[NormalizedLink] = []
    for item in items:
        link = item if isinstance(item, NormalizedLink) else NormalizedLink.model_validate(item)
        key = link.url
        if key in seen:
            continue
        seen.add(key)
        result.append(link)
    return result
