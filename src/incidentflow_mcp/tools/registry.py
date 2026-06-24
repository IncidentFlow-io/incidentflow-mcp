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
    title: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


_READ_ONLY_LOCAL_ANNOTATIONS = {
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}


_READ_ONLY_LOCAL_JUSTIFICATION = (
    "This tool only retrieves or computes operational information. It does not create, "
    "update, delete, restart, scale, patch, send messages, or perform irreversible actions."
)

_K8S_READ_ONLY_JUSTIFICATION = (
    "This tool performs read-only inspection through the IncidentFlow Kubernetes Agent. "
    "It may query Kubernetes API resources such as Pods, Events, Deployments, Services, "
    "Namespaces, rollout status, or redacted logs, but it never modifies Kubernetes resources."
)

_SLACK_READ_ONLY_JUSTIFICATION = (
    "This tool reads Slack alert messages or threads for incident analysis. It does not post "
    "messages, update messages, delete messages, invite users, or change Slack workspace "
    "configuration."
)

_STATUS_READ_ONLY_JUSTIFICATION = (
    "This tool reads public provider status information and does not modify any external "
    "provider state."
)


def _read_only_annotations() -> dict[str, Any]:
    return dict(_READ_ONLY_LOCAL_ANNOTATIONS)


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
        title="Summarize Incident",
        description=(
            "Reads IncidentFlow incident data and returns a structured summary with title, "
            "severity, status, affected services, event timeline, and remediation "
            f"recommendations. {_READ_ONLY_LOCAL_JUSTIFICATION}"
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
                    "description": (
                        "Execution strategy. auto => async in production, sync elsewhere."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for async orchestration. Optional when token has "
                        "workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": ["incident_id"],
        },
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="correlate_alerts",
        title="Correlate Alerts",
        description=(
            "Computes read-only alert correlation from the provided alert payload, grouping "
            "alerts by shared service, label affinity, and time proximity. Returns cluster "
            "assignments, dominant severity, likely root cause, and confidence score without "
            f"modifying any source system. {_READ_ONLY_LOCAL_JUSTIFICATION}"
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
                    "description": "Correlation time window in minutes (1-1440)",
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
                    "description": (
                        "Execution strategy. auto => async in production, sync elsewhere."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for async orchestration. Optional when token has "
                        "workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": ["alerts_json"],
        },
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="external_status_check",
        title="Check External Service Status",
        description=(
            "Checks recent public service status for supported external providers such as "
            "GitHub or AWS and returns current status, incidents, and historical summaries. "
            "Default response_mode=compact returns a chat-safe summary; response_mode=full "
            f"returns the complete provider payload. {_STATUS_READ_ONLY_JUSTIFICATION}"
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
                    "description": (
                        "If true (default), polls until the job completes. If false, returns "
                        "job_id immediately for manual polling."
                    ),
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["auto", "sync", "async"],
                    "default": "async",
                    "description": "Runner orchestration mode. auto/sync are coerced to async.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for async orchestration. Optional when token has "
                        "workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured."
                    ),
                },
                "check_id": {
                    "type": "string",
                    "description": (
                        "Existing async job_id for polling. When provided, MCP polls this "
                        "job and does not create a new one."
                    ),
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": (
                        "compact returns chat-safe summary; full returns raw job result payload."
                    ),
                },
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="slack_alerts_list",
        title="Review Slack Alerts",
        description=(
            "Reads recent alert messages from a configured Slack alert channel and parses "
            "Grafana or Alertmanager-style payloads into structured incident context with "
            "status, labels, summaries, timestamps, and Slack permalinks. "
            f"{_SLACK_READ_ONLY_JUSTIFICATION}"
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
                    "description": (
                        "none returns alert messages only; metadata returns thread "
                        "counts/users; full fetches replies and analysis."
                    ),
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
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="slack_alert_thread_get",
        title="Read Slack Alert Thread",
        description=(
            "Reads a Slack alert message thread by channel_id and message_ts/thread_ts and "
            "returns parsed engineer replies, commands, links, hypotheses, decisions, and "
            f"possible resolution signals. {_SLACK_READ_ONLY_JUSTIFICATION}"
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
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="incident_thread_summary",
        title="Summarize Incident Thread",
        description=(
            "Given Slack alert context, reads the related Slack thread and produces an "
            "SRE-focused human-context summary without executing suggested commands or "
            f"changing Slack data. {_SLACK_READ_ONLY_JUSTIFICATION}"
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
                    "description": (
                        "Optional alert or incident context to shape the summary "
                        "title/root-cause hints."
                    ),
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
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_agent_command",
        title="Run Read-Only Kubernetes Inspection",
        description=(
            "Dispatches one allowlisted read-only Kubernetes inspection command to an "
            "online IncidentFlow Kubernetes Agent through platform-api and agent-gateway. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
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
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_connection_health",
        title="Check Kubernetes Connection",
        description=(
            "Checks whether the IncidentFlow Kubernetes Agent is online, the cluster is "
            "reachable, and core read-only permissions work. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_cluster_overview",
        title="Inspect Kubernetes Cluster Health",
        description=(
            "Returns a read-only SRE overview of visible namespaces, pods, deployments, "
            "services, warning events, unhealthy pods, and restarts through the "
            f"IncidentFlow Kubernetes Agent. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_namespace_overview",
        title="Inspect Kubernetes Namespace",
        description=(
            "Returns a read-only SRE overview scoped to one allowed Kubernetes namespace, "
            "including Pods, Events, Deployments, Services, health signals, and restarts. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}, required=["namespace"]),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_rbac_check",
        title="Check Kubernetes Read-Only Permissions",
        description=(
            "Checks the read-only Kubernetes permissions available through the IncidentFlow "
            "Kubernetes Agent and returns allowed inspection capabilities. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_agent_status",
        title="Check Kubernetes Agent Status",
        description=(
            "Returns Kubernetes agent registry status, version, heartbeat, and selected "
            "cluster identity without dispatching a Kubernetes command or modifying "
            f"cluster resources. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_list_namespaces",
        title="List Kubernetes Namespaces",
        description=(
            "Lists namespaces visible to an online IncidentFlow Kubernetes Agent using "
            f"read-only Kubernetes API access. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_list_pods",
        title="List Kubernetes Pods",
        description=(
            "Lists Pods in an allowed namespace through an online IncidentFlow Kubernetes "
            f"Agent and returns current Pod status metadata. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_get_pod",
        title="Inspect Kubernetes Pod",
        description=(
            "Reads details for one Pod in an allowed namespace through an online "
            "IncidentFlow Kubernetes Agent and returns status, containers, and metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {"namespace": {"type": "string"}, "pod": {"type": "string"}},
            required=["namespace", "pod"],
        ),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_get_pod_logs",
        title="Read Kubernetes Pod Logs",
        description=(
            "Reads redacted logs from a selected Kubernetes Pod in an allowed namespace "
            "through the IncidentFlow Kubernetes Agent. This tool is read-only and does "
            "not modify Pods, containers, Deployments, or cluster configuration. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
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
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_list_events",
        title="List Kubernetes Events",
        description=(
            "Lists Kubernetes Events in an allowed namespace through an online IncidentFlow "
            "Kubernetes Agent and returns warning and normal event metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_list_deployments",
        title="List Kubernetes Deployments",
        description=(
            "Lists Deployments in an allowed namespace through an online IncidentFlow "
            "Kubernetes Agent and returns current rollout and availability metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_list_services",
        title="List Kubernetes Services",
        description=(
            "Lists Services in an allowed namespace through an online IncidentFlow "
            "Kubernetes Agent and returns service type, ports, selectors, and metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_get_rollout_status",
        title="Check Deployment Rollout",
        description=(
            "Reads Deployment rollout status through an online IncidentFlow Kubernetes "
            "Agent and returns readiness, availability, and rollout progress. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {"namespace": {"type": "string"}, "deployment": {"type": "string"}},
            required=["namespace", "deployment"],
        ),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_show_namespaces",
        title="Show Kubernetes Namespaces",
        description=(
            "Shows Kubernetes namespaces using automatic cluster resolution through the "
            f"IncidentFlow Kubernetes Agent. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_show_pods",
        title="Show Kubernetes Pods",
        description=(
            "Shows Kubernetes Pods using automatic cluster resolution through the "
            "IncidentFlow Kubernetes Agent and returns current Pod status metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_show_unhealthy_pods",
        title="Find Unhealthy Kubernetes Pods",
        description=(
            "Finds Kubernetes Pods that are not running, not ready, crash looping, "
            "pending, failed, or have recent container restarts. This tool performs "
            "read-only inspection through the IncidentFlow Kubernetes Agent and does not "
            "create, update, delete, restart, scale, or patch any Kubernetes resources. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
    ),
    ToolSpec(
        name="k8s_analyze_workload",
        title="Analyze Kubernetes Workload",
        description=(
            "Inspects a Kubernetes workload rollout, related Pods, unhealthy status, and "
            "redacted recent logs through the IncidentFlow Kubernetes Agent. This tool is "
            "read-only and does not restart, scale, patch, delete, or update Kubernetes "
            f"resources. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "workload": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
            },
            required=["namespace", "workload"],
        ),
        annotations=_read_only_annotations(),
    ),
]


def get_tool_specs() -> list[ToolSpec]:
    """Return all registered tool specifications."""
    return list(_TOOL_SPECS)
