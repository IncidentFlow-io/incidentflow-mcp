"""
Single source of truth for all MCP tool metadata.

Both the MCP server (mcp/server.py) and the CLI (cli/main.py) read from this
module so that tool names, descriptions, input schemas, and annotations are
never duplicated.

To add a new tool:
  1. Append a ToolSpec entry to _TOOL_SPECS.
  2. Register the implementation in mcp/server.py using the same name.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="incident_summary",
        description=(
            "Return a structured summary for a given incident, including title, "
            "severity, status, affected services, event timeline, and remediation "
            "recommendations."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "Unique incident identifier (e.g. INC-001)",
                },
                "include_timeline": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include event timeline in the response",
                },
                "include_affected_services": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include impacted service list in the response",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["auto", "sync", "async"],
                    "default": "auto",
                    "description": "Execution strategy. auto => async in production, sync elsewhere.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace scope for async orchestration. Optional when token has workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured.",
                },
            },
            "required": ["incident_id"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    ),
    ToolSpec(
        name="correlate_alerts",
        description=(
            "Group a list of incoming alerts into correlated clusters based on "
            "shared service, label affinity, and time proximity. Returns cluster "
            "assignments, dominant severity, likely root cause, and confidence score."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "alerts_json": {
                    "type": "string",
                    "description": "JSON array of Alert objects to correlate",
                },
                "window_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Correlation time window in minutes (1–1440)",
                },
                "min_cluster_size": {
                    "type": "integer",
                    "default": 2,
                    "description": "Minimum number of alerts required to form a cluster",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["auto", "sync", "async"],
                    "default": "auto",
                    "description": "Execution strategy. auto => async in production, sync elsewhere.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace scope for async orchestration. Optional when token has workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured.",
                },
            },
            "required": ["alerts_json"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    ),
    ToolSpec(
        name="external_status_check",
        description=(
            "Fetch real-time and historical AWS/GitHub status via async jobs. "
            "Default response_mode=compact returns a chat-safe summary. Use "
            "response_mode=full for complete raw payload (including larger data such as "
            "incident updates). Set wait_for_result=false to get an async job_id for "
            "manual polling."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "providers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["aws", "github"]},
                    "default": ["aws", "github"],
                    "description": "External status providers to query",
                },
                "days_back": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 365,
                    "description": "How many days of incident history to fetch (default: 30)",
                },
                "wait_for_result": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), polls until the job completes. If false, returns job_id immediately for manual polling.",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["auto", "sync", "async"],
                    "default": "async",
                    "description": "Runner orchestration mode. auto/sync are coerced to async.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace scope for async orchestration. Optional when token has workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured.",
                },
                "check_id": {
                    "type": "string",
                    "description": "Existing async job_id for polling (when provided, MCP polls this job and does not create a new one)",
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact returns chat-safe summary; full returns raw job result payload.",
                },
            },
            "required": [],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="slack_alerts_list",
        description=(
            "Read recent alert messages from a Slack alert channel, parse Grafana/"
            "Alertmanager-style payloads, and return a structured JSON list with "
            "status, labels, summaries, timestamps, and Slack permalinks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "default": "alerts",
                    "description": "Slack channel name (#alerts or alerts) or channel ID.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Number of recent channel messages to inspect.",
                },
                "include_raw": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include extracted raw Slack text in each parsed alert.",
                },
                "include_threads": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable Slack thread metadata or full thread enrichment.",
                },
                "thread_mode": {
                    "type": "string",
                    "enum": ["none", "metadata", "full"],
                    "default": "none",
                    "description": "none returns alert messages only; metadata returns thread counts/users; full fetches replies and analysis.",
                },
                "max_thread_replies": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 0,
                    "maximum": 200,
                    "description": "Maximum thread replies to fetch when thread_mode=full.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for platform Slack mode. Optional when "
                        "INCIDENTFLOW_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": [],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="slack_alert_thread_get",
        description=(
            "Read a Slack alert message thread by channel_id and message_ts/thread_ts "
            "and return parsed engineer replies, commands, links, hypotheses, decisions, "
            "and possible resolution signals."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID containing the alert message.",
                },
                "message_ts": {
                    "type": "string",
                    "description": "Slack root message timestamp or thread_ts.",
                },
                "include_root": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include parsed root alert details in the response.",
                },
                "max_replies": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 0,
                    "maximum": 200,
                    "description": "Maximum Slack thread replies to fetch.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for platform Slack mode. Optional when "
                        "INCIDENTFLOW_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": ["channel_id", "message_ts"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="incident_thread_summary",
        description=(
            "Given Slack alert context, read the related Slack thread and produce an "
            "SRE-focused human-context summary without executing any suggested command."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID containing the thread.",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Slack thread timestamp.",
                },
                "alert_context": {
                    "type": "object",
                    "description": "Optional alert or incident context to shape the summary title/root-cause hints.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for platform Slack mode. Optional when "
                        "INCIDENTFLOW_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": ["channel_id", "thread_ts"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
]


def get_tool_specs() -> list[ToolSpec]:
    """Return all registered tool specifications."""
    return list(_TOOL_SPECS)
