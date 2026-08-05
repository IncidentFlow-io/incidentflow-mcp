"""Registration for Slack and incident-thread MCP tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.errors import structured_guard_error
from incidentflow_mcp.mcp.services.memory_context import MemoryContextService, spawn_background_task
from incidentflow_mcp.platform_api.slack_client import PlatformSlackAPIError, PlatformSlackClient
from incidentflow_mcp.tools.slack_alerts import (
    fetch_slack_alert_thread,
    fetch_slack_alerts,
    summarize_incident_thread,
)

_VALID_SLACK_THREAD_MODES = {"none", "metadata", "full"}
_SLACK_THREAD_MODE_ALIASES = {
    "summarize": "full",
    "summary": "full",
    "analysis": "full",
    "analyze": "full",
}

ToolGuardResolver = Callable[[str], Awaitable[object]]
TokenWorkspaceResolver = Callable[[], str | None]
SlackAccessResolver = Callable[
    [str | None, str],
    tuple[str | None, PlatformSlackClient | None],
]
ErrorJsonBuilder = Callable[[], str]
SlackErrorJsonBuilder = Callable[[PlatformSlackAPIError], str]


class IncidentThreadAlertContext(BaseModel):
    alert_name: str | None = Field(
        default=None,
        description="Alert name from the root Slack alert, for example InstanceDown.",
    )
    name: str | None = Field(default=None, description="Alternative alert name field.")
    summary: str | None = Field(default=None, description="Short alert or incident summary.")
    service: str | None = Field(default=None, description="Affected service name.")
    severity: str | None = Field(
        default=None,
        description="Alert severity such as critical or warning.",
    )
    status: str | None = Field(default=None, description="Alert status such as firing or resolved.")
    labels: dict[str, str] | None = Field(
        default=None,
        description="Alert labels copied from Grafana, Alertmanager, or IncidentFlow.",
    )


def normalize_slack_thread_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    mode = _SLACK_THREAD_MODE_ALIASES.get(mode, mode)
    if mode not in _VALID_SLACK_THREAD_MODES:
        raise ValueError(
            "thread_mode must be one of: none, metadata, full "
            "(summarize and analysis are accepted aliases for full)"
        )
    return mode


def register_slack_tools(
    ctx: ToolRegistrationContext,
    *,
    memory: MemoryContextService,
    resolve_tool_guard: ToolGuardResolver,
    current_token_workspace_id: TokenWorkspaceResolver,
    resolve_slack_access: SlackAccessResolver,
    workspace_context_required_error: ErrorJsonBuilder,
    platform_slack_error_json: SlackErrorJsonBuilder,
) -> None:
    @ctx.mcp.tool(**ctx.metadata("slack_alerts_list"))
    async def slack_alerts_list(
        channel: str | None = None,
        limit: int | None = None,
        include_raw: bool = False,
        include_threads: bool = False,
        thread_mode: str = "none",
        max_thread_replies: int = 20,
        include_system_messages: bool = False,
        deduplicate: bool = True,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("slack_alerts_list")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        token_workspace_id = current_token_workspace_id()
        if not token_workspace_id:
            return structured_guard_error(workspace_context_required_error())

        try:
            selected_channel = (channel or ctx.settings.slack_alerts_channel).strip() or "alerts"
            selected_limit = ctx.settings.slack_alerts_default_limit if limit is None else limit
            if selected_limit < 1 or selected_limit > 200:
                raise ValueError("limit must be between 1 and 200")
            selected_thread_mode = normalize_slack_thread_mode(thread_mode)
            if max_thread_replies < 0 or max_thread_replies > 200:
                raise ValueError("max_thread_replies must be between 0 and 200")
            token, platform_client = resolve_slack_access(workspace_id, token_workspace_id)

            result = await fetch_slack_alerts(
                token=token,
                channel=selected_channel,
                limit=selected_limit,
                include_raw=include_raw,
                include_threads=include_threads,
                thread_mode=selected_thread_mode,  # type: ignore[arg-type]
                max_thread_replies=max_thread_replies,
                include_system_messages=include_system_messages,
                deduplicate=deduplicate,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return structured_guard_error(platform_slack_error_json(exc))
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("slack_alert_thread_get"))
    async def slack_alert_thread_get(
        channel_id: str,
        message_ts: str,
        include_root: bool = True,
        include_raw: bool = False,
        max_replies: int = 50,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("slack_alert_thread_get")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        token_workspace_id = current_token_workspace_id()
        if not token_workspace_id:
            return structured_guard_error(workspace_context_required_error())

        try:
            if max_replies < 0 or max_replies > 200:
                raise ValueError("max_replies must be between 0 and 200")
            token, platform_client = resolve_slack_access(workspace_id, token_workspace_id)

            result = await fetch_slack_alert_thread(
                token=token,
                channel_id=channel_id,
                message_ts=message_ts,
                include_root=include_root,
                include_raw=include_raw,
                max_replies=max_replies,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return structured_guard_error(platform_slack_error_json(exc))
        return result.model_dump(mode="json")

    @ctx.mcp.tool(**ctx.metadata("incident_thread_summary"))
    async def incident_thread_summary(
        channel_id: str,
        thread_ts: str,
        alert_context: IncidentThreadAlertContext | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        guard = await resolve_tool_guard("incident_thread_summary")
        if isinstance(guard, str):
            return structured_guard_error(guard)
        token_workspace_id = current_token_workspace_id()
        if not token_workspace_id:
            return structured_guard_error(workspace_context_required_error())

        try:
            token, platform_client = resolve_slack_access(workspace_id, token_workspace_id)

            result = await summarize_incident_thread(
                token=token,
                channel_id=channel_id,
                thread_ts=thread_ts,
                alert_context=alert_context.model_dump(exclude_none=True)
                if alert_context
                else None,
                client=platform_client,
            )
        except PlatformSlackAPIError as exc:
            return structured_guard_error(platform_slack_error_json(exc))

        spawn_background_task(
            memory.auto_upsert_thread_summary(
                workspace_id=token_workspace_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                result=result,
                alert_context=alert_context,
            )
        )

        return result
