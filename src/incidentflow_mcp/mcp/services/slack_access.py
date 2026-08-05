"""Slack access resolution for MCP Slack tools."""

from __future__ import annotations

import json
from dataclasses import dataclass

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.services.async_jobs import resolve_job_workspace_id
from incidentflow_mcp.platform_api.slack_client import PlatformSlackAPIError, PlatformSlackClient


def platform_slack_mode_enabled(settings: Settings) -> bool:
    return bool(settings.platform_api_base_url and settings.platform_api_internal_api_key)


def resolve_slack_tool_access(
    settings: Settings,
    *,
    workspace_id: str | None,
    token_workspace_id: str,
) -> tuple[str | None, PlatformSlackClient | None]:
    resolved_workspace_id = resolve_job_workspace_id(
        workspace_id,
        token_workspace_id=token_workspace_id,
        default_workspace_id=settings.mcp_default_workspace_id,
    )

    if platform_slack_mode_enabled(settings):
        return None, PlatformSlackClient(settings, workspace_id=resolved_workspace_id)

    if settings.environment == "production":
        raise ValueError(
            "slack_platform_mode_required: configure PLATFORM_API_BASE_URL and "
            "PLATFORM_API_INTERNAL_TOKEN for Slack MCP tools in production"
        )

    token = settings.slack_bot_token
    if token is None:
        raise ValueError(
            "slack_token_missing: configure platform Slack mode or local SLACK_BOT_TOKEN"
        )

    return token.get_secret_value(), None


@dataclass(frozen=True, slots=True)
class SlackAccessResolver:
    settings: Settings

    def resolve(
        self,
        workspace_id: str | None,
        token_workspace_id: str,
    ) -> tuple[str | None, PlatformSlackClient | None]:
        return resolve_slack_tool_access(
            self.settings,
            workspace_id=workspace_id,
            token_workspace_id=token_workspace_id,
        )


def tool_error_json(code: str, message: str, **details: object) -> str:
    payload: dict[str, object] = {"error": code, "code": code, "message": message}
    if details:
        payload["details"] = details
    return json.dumps(payload, indent=2)


def workspace_context_required_error() -> str:
    return tool_error_json(
        "mcp_workspace_context_required",
        (
            "MCP Slack tools require an OAuth or workspace token with workspace_id. "
            "Authorize the MCP client through IncidentFlow OAuth and retry."
        ),
    )


def platform_slack_error_json(exc: PlatformSlackAPIError) -> str:
    return tool_error_json(exc.code, exc.message)
