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


def _k8s_cluster_properties() -> dict[str, Any]:
    return {
        "environment": {
            "type": "string",
            "description": "Optional environment selector, e.g. production, staging, or dev.",
        },
        "cluster_name": {
            "type": "string",
            "description": "Optional cluster name or alias selector.",
        },
        "cluster_id": {
            "type": "string",
            "description": "Internal/debug override. Usually omit this.",
        },
    }


def _timeout_property() -> dict[str, Any]:
    return {"type": "integer", "default": 30, "minimum": 1, "maximum": 60}


def _k8s_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            **_k8s_cluster_properties(),
            **(properties or {}),
            "timeout_seconds": _timeout_property(),
        },
        "required": required or [],
    }


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
    ToolSpec(
        name="k8s_agent_command",
        description=(
            "Dispatch a read-only Kubernetes inspection command to an online "
            "IncidentFlow Kubernetes Agent through platform-api and agent-gateway."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_k8s_cluster_properties(),
                "action": {
                    "type": "string",
                    "enum": [
                        "k8s.list_namespaces",
                        "k8s.list_pods",
                        "k8s.get_pod",
                        "k8s.get_pod_logs",
                        "k8s.list_events",
                        "k8s.list_deployments",
                        "k8s.list_services",
                        "k8s.get_rollout_status",
                    ],
                },
                "params": {"type": "object", "default": {}},
                "timeout_seconds": _timeout_property(),
            },
            "required": ["action"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    ),
    ToolSpec(
        name="k8s_list_namespaces",
        description="List namespaces visible to an online IncidentFlow Kubernetes Agent.",
        input_schema=_k8s_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_list_pods",
        description="List pods in an allowed namespace through an online Kubernetes Agent.",
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_get_pod",
        description="Get one pod in an allowed namespace through an online Kubernetes Agent.",
        input_schema=_k8s_schema(
            {"namespace": {"type": "string"}, "pod": {"type": "string"}},
            required=["namespace", "pod"],
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_get_pod_logs",
        description=(
            "Read redacted pod logs in an allowed namespace through an online "
            "Kubernetes Agent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_k8s_cluster_properties(),
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "container": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
                "timeout_seconds": _timeout_property(),
            },
            "required": ["namespace", "pod"],
        },
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_list_events",
        description=(
            "List Kubernetes events in an allowed namespace through an online "
            "Kubernetes Agent."
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_list_deployments",
        description="List deployments in an allowed namespace through an online Kubernetes Agent.",
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_list_services",
        description="List services in an allowed namespace through an online Kubernetes Agent.",
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_get_rollout_status",
        description="Get deployment rollout status through an online Kubernetes Agent.",
        input_schema=_k8s_schema(
            {"namespace": {"type": "string"}, "deployment": {"type": "string"}},
            required=["namespace", "deployment"],
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_show_namespaces",
        description="Show Kubernetes namespaces using automatic cluster resolution.",
        input_schema=_k8s_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_show_pods",
        description="Show Kubernetes pods using automatic cluster resolution.",
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_show_unhealthy_pods",
        description="Show pods that are not running, not ready, or have restarts.",
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="k8s_analyze_workload",
        description="Inspect a Kubernetes workload rollout and related pods.",
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "workload": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
            },
            required=["namespace", "workload"],
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    ToolSpec(
        name="grafana_list_dashboards",
        description=(
            "List the Grafana dashboards approved (allow-listed) for this workspace, "
            "with uid, title, folder, and tags. Use the uid with the other grafana tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope. Optional when the token has workspace scope "
                        "or INCIDENTFLOW_WORKSPACE_ID is configured."
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
        name="grafana_get_dashboard",
        description=(
            "Fetch a single allow-listed Grafana dashboard's metadata (panels, "
            "datasources) by uid."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {"type": "string", "description": "Grafana dashboard uid."},
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["dashboard_uid"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="grafana_extract_panel_queries",
        description=(
            "Extract the Prometheus/PromQL queries from a dashboard's panels (rows "
            "traversed, non-Prometheus targets skipped). Returns panel title, refId, "
            "datasource uid, and the expression."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {"type": "string", "description": "Grafana dashboard uid."},
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["dashboard_uid"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="grafana_metrics_query",
        description=(
            "Run an instant PromQL query through Grafana's datasource proxy. The query "
            "is validated server-side (allow-listed metrics, shape limits) and the result "
            "is returned as normalized, label-sanitized series."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "datasource_uid": {"type": "string", "description": "Grafana datasource uid."},
                "query": {"type": "string", "description": "PromQL expression."},
                "time": {
                    "type": "string",
                    "description": "Optional evaluation time (RFC3339 or unix seconds).",
                },
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["datasource_uid", "query"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="grafana_metrics_query_range",
        description=(
            "Run a range PromQL query through Grafana's datasource proxy over [start, end] "
            "at a given step. Validated server-side; returns normalized, label-sanitized "
            "time series."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "datasource_uid": {"type": "string", "description": "Grafana datasource uid."},
                "query": {"type": "string", "description": "PromQL expression."},
                "start": {"type": "string", "description": "Range start (RFC3339/unix/now-6h)."},
                "end": {"type": "string", "description": "Range end (RFC3339/unix/now)."},
                "step": {"type": "string", "description": "Step, e.g. '30s' or seconds."},
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["datasource_uid", "query", "start", "end", "step"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    ToolSpec(
        name="analyze_dashboard_health",
        description=(
            "Analyze an allow-listed Grafana dashboard over a time window: extracts each "
            "panel's PromQL, runs it (guardrail-checked), and returns per-panel normalized "
            "series with anomaly flags and a summary. Read-only; suggests no actions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {"type": "string", "description": "Grafana dashboard uid."},
                "start": {
                    "type": "string",
                    "default": "now-6h",
                    "description": "Window start (default now-6h).",
                },
                "end": {
                    "type": "string",
                    "default": "now",
                    "description": "Window end (default now).",
                },
                "step": {"type": "string", "description": "Optional step; server picks a default."},
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["dashboard_uid"],
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
