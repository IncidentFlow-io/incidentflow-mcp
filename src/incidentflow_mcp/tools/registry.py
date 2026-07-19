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

from incidentflow_mcp.integrations import IntegrationType


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    structured_output: bool | None = None
    required_integration: IntegrationType | None = None
    supports_shared_dev_fallback: bool = False

    def __post_init__(self) -> None:
        if self.required_integration is None:
            if self.name.startswith("k8s_"):
                self.required_integration = "kubernetes"
                self.supports_shared_dev_fallback = True
            elif self.name.startswith("grafana_") or self.name == "analyze_dashboard_health":
                self.required_integration = "grafana"
            elif self.name.startswith("slack_") or self.name == "incident_thread_summary":
                self.required_integration = "slack"
            elif self.name.startswith("argocd_"):
                self.required_integration = "argocd"


_READ_ONLY_LOCAL_ANNOTATIONS = {
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}


# These justifications are written in positive, capability-free language on purpose.
# Enumerating what a tool "does not do" (exec, shell, escalate privileges, delete, ...)
# reads as a list of dangerous capabilities to content-safety classifiers, which handle
# negation poorly and can raise a read-only tool's risk score. Describe only what the
# tool retrieves and that it is read-only.
_READ_ONLY_LOCAL_JUSTIFICATION = (
    "Read-only tool: it retrieves or computes operational information and returns it as "
    "structured data. It is limited to reading and cannot make any changes."
)

_K8S_READ_ONLY_JUSTIFICATION = (
    "Read-only observability tool: it reads Kubernetes resource status and metadata from "
    "the cluster API and returns it as structured data. It does not exec into containers, "
    "run shell commands, restart, scale, patch, delete, update resources, or escalate "
    "privileges."
)

_SLACK_READ_ONLY_JUSTIFICATION = (
    "Read-only tool: it reads Slack alert messages and threads for incident analysis and "
    "returns them as structured data. It is limited to reading and cannot make any changes."
)

_STATUS_READ_ONLY_JUSTIFICATION = (
    "Read-only tool: it reads public provider status information and returns it as "
    "structured data. It is limited to reading and cannot make any changes."
)


def _read_only_annotations() -> dict[str, Any]:
    return dict(_READ_ONLY_LOCAL_ANNOTATIONS)


def build_tool_description(spec: ToolSpec, *, environment: str = "development") -> str:
    if not spec.required_integration:
        return spec.description

    label = {
        "kubernetes": "Kubernetes",
        "grafana": "Grafana",
        "slack": "Slack",
        "argocd": "Argo CD",
    }[spec.required_integration]
    description = (
        f"{spec.description} Requires an active {label} integration in the current workspace."
    )
    if (
        spec.required_integration == "kubernetes"
        and spec.supports_shared_dev_fallback
        and environment in {"dev", "development", "test", "local"}
    ):
        description += (
            " In development environments, a shared sandbox cluster may be used when "
            "explicitly enabled."
        )
    return description


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


def _alert_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "alert_id": {"type": "string", "description": "Stable alert identifier."},
            "name": {"type": "string", "description": "Alert name, for example InstanceDown."},
            "service": {"type": "string", "description": "Affected service name."},
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "warning", "low", "info"],
            },
            "status": {"type": "string", "enum": ["firing", "resolved", "pending"]},
            "fired_at": {
                "type": "string",
                "format": "date-time",
                "description": "Time the alert fired, as an ISO 8601 timestamp.",
            },
            "labels": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Optional alert labels such as env, namespace, pod, or deployment.",
            },
            "slack": {
                "type": "object",
                "description": "Optional Slack message metadata for the alert.",
            },
            "thread": {
                "type": "object",
                "description": "Optional Slack thread metadata for the alert.",
            },
        },
        "required": ["alert_id", "name", "service", "severity", "status", "fired_at"],
    }


def _alert_context_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "alert_name": {
                "type": "string",
                "description": "Alert name from the root Slack alert.",
            },
            "name": {"type": "string", "description": "Alternative alert name field."},
            "summary": {"type": "string", "description": "Short alert or incident summary."},
            "service": {"type": "string", "description": "Affected service name."},
            "severity": {"type": "string", "description": "Alert severity."},
            "status": {"type": "string", "description": "Alert status."},
            "labels": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Alert labels copied from Grafana, Alertmanager, or IncidentFlow.",
            },
        },
    }


def _k8s_agent_params_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": "Namespace for namespaced Kubernetes inspection actions.",
            },
            "pod": {"type": "string", "description": "Pod name for get_pod or get_pod_logs."},
            "container": {"type": "string", "description": "Optional container for pod logs."},
            "deployment": {
                "type": "string",
                "description": "Deployment name for get_rollout_status.",
            },
            "tail_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "Maximum recent log lines for get_pod_logs.",
            },
        },
    }


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
        name="incidentflow_capabilities",
        title="List IncidentFlow Capabilities",
        description=(
            "Returns the canonical IncidentFlow MCP capability inventory from the tool "
            "registry, grouped by operational category with tool names, descriptions, and "
            "safety metadata. Use this for deterministic tool discovery without search "
            f"ranking or result limits. {_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact omits long descriptions; full includes full metadata.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional category id filter, e.g. kubernetes, grafana_prometheus, "
                        "slack_incidents, or knowledge."
                    ),
                },
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="mcp_version",
        title="Show MCP Server Version",
        description=(
            "Returns the IncidentFlow MCP server version, release tag, build commit, "
            "container image provenance, deployment environment, registered tool counts, "
            "and server identity. Use this to verify which MCP build is currently serving "
            "tools after a deployment. "
            f"{_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="incidentflow_auth_status",
        title="Show IncidentFlow Auth Status",
        description=(
            "Show the authenticated IncidentFlow user, active workspace, workspace role, "
            "and runtime environment. "
            f"{_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="incidentflow_integrations_status",
        title="Show IncidentFlow Integrations Status",
        description=(
            "Show which IncidentFlow integrations are connected for the active workspace "
            "and whether a development fallback is being used. "
            f"{_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="public_knowledge_search",
        title="Search Public Knowledge",
        description=(
            "Search public IncidentFlow documentation and product knowledge. Use this for "
            "setup, integrations, MCP usage, API references, tool guidance, runbooks, "
            "security docs, and troubleshooting content that is not workspace-private. "
            f"{_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language public knowledge query.",
                },
                "document_type": {
                    "type": "string",
                    "enum": [
                        "documentation",
                        "integration_guide",
                        "tool_reference",
                        "runbook",
                        "troubleshooting",
                        "best_practice",
                        "security_documentation",
                        "faq",
                        "api_reference",
                    ],
                    "description": "Optional public document type filter.",
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact returns excerpts only; full includes complete content.",
                },
                "limit": {
                    "type": "integer",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="private_knowledge_search",
        title="Search Private Knowledge",
        description=(
            "Search authenticated workspace knowledge and memory, including incidents, RCA, "
            "postmortems, runbooks, service context, deployment notes, and operational "
            "knowledge. Workspace scope is resolved from the authenticated user or the "
            f"configured development workspace. {_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language private query."},
                "document_type": {
                    "type": "string",
                    "enum": [
                        "incident",
                        "rca",
                        "postmortem",
                        "runbook",
                        "knowledge",
                        "service_context",
                        "deployment_note",
                    ],
                    "description": "Optional workspace document type filter.",
                },
                "service": {"type": "string", "description": "Optional service filter."},
                "environment": {
                    "type": "string",
                    "description": "Optional workspace environment filter.",
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact returns excerpts only; full includes complete content.",
                },
                "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="knowledge_get",
        title="Get Knowledge Document",
        description=(
            "Retrieve one private workspace knowledge document deterministically by UI UUID, "
            "database entry id, semantic point id, exact title, or stable document/knowledge "
            f"id. Use this after a search result or browser URL identifies a document. "
            f"{_READ_ONLY_LOCAL_JUSTIFICATION}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "UI UUID, entry id, point id, exact title, or stable document id."
                    ),
                },
                "id_type": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "entry_id",
                        "ui_id",
                        "knowledge_id",
                        "document_id",
                        "point_id",
                        "title",
                    ],
                    "default": "auto",
                    "description": "Identifier type. auto tries deterministic exact lookups.",
                },
                "document_type": {
                    "type": "string",
                    "enum": ["incident", "rca", "postmortem", "runbook", "knowledge"],
                    "description": "Optional type hint for stable document ids or title matches.",
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "full",
                    "description": "compact omits document body; full includes complete content.",
                },
            },
            "required": ["id"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
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
                    "description": (
                        "Unique incident identifier (e.g. INC-001). Required unless check_id "
                        "is provided to poll an existing async job."
                    ),
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
                        "Execution strategy. sync runs the read-only correlator inline and "
                        "returns the summary immediately; async dispatches a runner job and "
                        "returns a job_id to poll via check_id; auto picks async when async "
                        "tools are enabled, else sync."
                    ),
                },
                "wait_for_result": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When polling with check_id: if true (default), polls until the job "
                        "completes; if false, returns the current job status immediately."
                    ),
                },
                "check_id": {
                    "type": "string",
                    "description": (
                        "Existing async job_id to poll. When provided, MCP fetches this job's "
                        "result instead of creating a new summary (incident_id not required)."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace scope for async orchestration. Optional when the token has "
                        "workspace scope or MCP_DEFAULT_WORKSPACE_ID is configured."
                    ),
                },
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
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
                "alerts": {
                    "type": "array",
                    "items": _alert_schema(),
                    "minItems": 1,
                    "maxItems": 500,
                    "description": (
                        "Alert objects to correlate. Each alert requires alert_id, name, "
                        "service, severity, status, and fired_at."
                    ),
                },
                "alerts_json": {
                    "type": "string",
                    "description": (
                        "Legacy JSON string containing the same array accepted by alerts. "
                        "Prefer alerts for new calls."
                    ),
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
                        "Execution strategy. auto and sync run inline. async is reserved "
                        "for a future dedicated correlation runner."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Reserved for future async orchestration; ignored for inline correlation."
                    ),
                },
            },
        },
        annotations=_read_only_annotations(),
        structured_output=True,
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
        structured_output=True,
    ),
    ToolSpec(
        name="slack_alerts_list",
        title="Review Slack Alerts",
        description=(
            "Reads recent alert messages from a configured Slack alert channel and parses "
            "Grafana or Alertmanager-style payloads into structured incident context with "
            "status, labels, summaries, timestamps, Slack permalinks, stable fingerprints, "
            "and occurrence counts. Repeated alert notifications are deduplicated by default. "
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
                "include_system_messages": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include Slack channel join/leave and other system messages.",
                },
                "deduplicate": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Group repeated notifications for the same alert fingerprint and "
                        "return first_seen, last_seen, and occurrences."
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
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
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
                "include_raw": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include unfiltered raw Slack message text in the root alert. "
                        "Default false returns a compact response: IPs are redacted and "
                        "commands are extracted into extracted_commands instead."
                    ),
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
        structured_output=True,
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
                    **_alert_context_schema(),
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
        structured_output=True,
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
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_cluster_overview",
        title="Inspect Kubernetes Cluster Health",
        description=(
            "Returns a read-only SRE overview of the cluster: pod health, unhealthy pods, "
            "deployment counts, warning events, and top restarts. Includes an automatic "
            "health assessment with findings and recommendations — Healthy, Degraded, or "
            f"Unknown. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(),
        annotations=_read_only_annotations(),
        structured_output=True,
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
        structured_output=True,
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
        structured_output=True,
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
        structured_output=True,
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
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_list_pods",
        title="Show Pod Health Status",
        description=(
            "Returns health and readiness status for running workload pods in a Kubernetes "
            "namespace — name, phase, container readiness, restart count, and age. "
            "Use during incident triage to check whether pods are Running, Pending, or "
            f"CrashLooping. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "include_labels": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include Kubernetes labels in the response. Off by default.",
                },
                "include_images": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include container image name:tag in the response.",
                },
                "include_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the node name the pod is scheduled on.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of pods to return. Defaults to 50.",
                },
            }
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_get_pod",
        title="Inspect Pod Status",
        description=(
            "Returns health status for a specific pod — container readiness, restart "
            "count, node assignment, and phase. Use when you know the pod name and need "
            "a quick health check. For full investigation use k8s_describe_pod. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "detail_level": {
                    "type": "string",
                    "enum": ["summary", "standard", "debug"],
                    "default": "summary",
                    "description": (
                        "summary=health basics only; standard=with events for this pod; "
                        "debug=standard plus raw agent response structure"
                    ),
                },
                "include_labels": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include Kubernetes labels in the response.",
                },
                "include_images": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include container image name:tag in the response.",
                },
                "include_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the node name the pod is scheduled on.",
                },
            },
            required=["namespace", "pod"],
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_get_pod_logs",
        title="Read Application Logs",
        description=(
            "Streams recent log lines from a running application container for incident "
            "debugging — equivalent to kubectl logs. Logs are filtered and redacted before "
            "return. No shell access, no exec, no writes. "
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
                "level": {
                    "type": "string",
                    "description": "Optional case-insensitive level filter.",
                },
                "contains": {
                    "type": "string",
                    "description": "Only return log lines containing this text.",
                },
                "exclude": {
                    "type": "string",
                    "description": "Drop log lines containing this text.",
                },
                "since_minutes": {"type": "integer", "minimum": 1},
                "compact": {
                    "type": "boolean",
                    "default": True,
                    "description": "Return compact highlighted logs instead of raw logs.",
                },
                "json_parse": {
                    "type": "boolean",
                    "default": False,
                    "description": "Ask the agent to parse JSON log lines when supported.",
                },
                "timeout_seconds": _timeout_property(),
            },
            "required": ["namespace", "pod"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_list_events",
        title="List Kubernetes Events",
        description=(
            "Lists Kubernetes Events in a namespace — warnings first, newest first. "
            "Repeated events are automatically deduplicated with occurrence counts. "
            "Useful for spotting CrashLoopBackOff, ImagePullBackOff, readiness failures, "
            f"and scheduling issues. {_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "pod": {
                    "type": "string",
                    "description": "Optional pod name to filter events to a specific pod.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of deduplicated events to return.",
                },
            }
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_list_deployments",
        title="List Kubernetes Deployments",
        description=(
            "Lists Deployments in an allowed namespace through an online IncidentFlow "
            "Kubernetes Agent and returns current rollout and availability metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of deployments to return.",
                },
            }
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_list_services",
        title="List Kubernetes Services",
        description=(
            "Lists Services in an allowed namespace through an online IncidentFlow "
            "Kubernetes Agent and returns service type, ports, selectors, and metadata. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of services to return.",
                },
            }
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
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
            {
                "namespace": {"type": "string"},
                "deployment": {
                    "type": "string",
                    "description": "Deployment name. Use 'workload' as an alias if preferred.",
                },
                "workload": {
                    "type": "string",
                    "description": "Alias for 'deployment'. Provide one or the other, not both.",
                },
            },
            required=["namespace"],
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_show_unhealthy_pods",
        title="Find Unhealthy Kubernetes Pods",
        description=(
            "Finds Kubernetes Pods that are not running, not ready, crash looping, "
            "pending, failed, or have high restart counts. Returns each unhealthy pod "
            "with its reason, restart count, age, likely cause, and recommended next "
            "action. Use as the first step in namespace-level triage. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema({"namespace": {"type": "string"}}),
        annotations=_read_only_annotations(),
        structured_output=True,
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
                "workload": {
                    "type": "string",
                    "description": (
                        "Deployment or Pod name to inspect, for example checkout-api or "
                        "checkout-api-7f9c6d7d8b-abcde. Do not include kind/ prefixes."
                    ),
                },
                "tail_lines": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
            },
            required=["namespace", "workload"],
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_describe_pod",
        title="Describe Pod",
        description=(
            "Returns a structured pod investigation report — the primary tool for "
            "understanding why a specific pod is unhealthy. Sections: pod identity "
            "(name, namespace, workload, node, age), status (phase, ready, restart count), "
            "containers (per-container readiness and image), relevant events (warnings "
            "first), and automatic diagnosis of CrashLoopBackOff, ImagePullBackOff, "
            "readiness/liveness failures, OOMKilled, FailedScheduling, and high restarts. "
            "Prefer this over k8s_get_pod when investigating a specific pod. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "include_details": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include lower-level pod details such as node, pod IP, probes, "
                        "and resource requests/limits. Off by default for compact output."
                    ),
                },
            },
            required=["namespace", "pod"],
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="k8s_debug_pod",
        title="Debug Pod",
        description=(
            "Runs a full automated investigation on a specific pod: describes pod state, "
            "reads recent logs, surfaces relevant Kubernetes events, and checks the "
            "owner deployment rollout status. Returns a single consolidated investigation "
            "report with health findings and actionable recommendations. Equivalent to an "
            "SRE running kubectl describe + logs + events + rollout status manually. "
            "Use when you need to understand why a pod is unhealthy or behaving unexpectedly. "
            f"{_K8S_READ_ONLY_JUSTIFICATION}"
        ),
        input_schema=_k8s_schema(
            {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "tail_lines": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Log lines to fetch for diagnosis.",
                },
            },
            required=["namespace", "pod"],
        ),
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_list_dashboards",
        title="List Grafana Dashboards",
        description=(
            "Use this when you need to discover which Grafana dashboards are approved for "
            "the current workspace before reading panels or running dashboard analysis. "
            "Returns allow-listed dashboard uid, title, folder, and tags. Read-only; access "
            "is mediated by platform-api workspace policy."
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
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_get_dashboard",
        title="Get Grafana Dashboard",
        description=(
            "Use this when you already have an allow-listed Grafana dashboard uid and need "
            "its dashboard metadata, panels, and datasource references. Defaults to "
            "response_mode=compact to keep panel payloads bounded. Read-only; the dashboard "
            "must be approved for the workspace in platform-api."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {
                    "type": "string",
                    "description": "Grafana dashboard UID, URL slug, or exact allow-listed title.",
                },
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims dashboard panels; full returns API payload.",
                },
                "panel_limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum panels returned in compact mode.",
                },
            },
            "required": ["dashboard_uid"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_extract_panel_queries",
        title="Extract Grafana Panel Queries",
        description=(
            "Use this when you need the Prometheus/PromQL expressions embedded in an "
            "allow-listed Grafana dashboard before deciding which metrics to query. "
            "Returns panel title, refId, datasource uid, and expression; skips unsupported "
            "or non-Prometheus targets."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {
                    "type": "string",
                    "description": "Grafana dashboard UID, URL slug, or exact allow-listed title.",
                },
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["dashboard_uid"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_metrics_query",
        title="Query Grafana Metrics",
        description=(
            "Use this when you need a point-in-time PromQL result from an approved Grafana "
            "datasource. Runs an instant query through platform-api, where PromQL guardrails, "
            "workspace policy, and label sanitization are enforced server-side. Defaults to "
            "response_mode=compact with bounded series and samples."
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
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims series/samples; full returns API payload.",
                },
                "max_series": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum metric series in compact mode.",
                },
                "max_points": {
                    "type": "integer",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum samples per series in compact mode.",
                },
            },
            "required": ["datasource_uid", "query"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_metrics_query_range",
        title="Query Grafana Metrics Range",
        description=(
            "Use this when you need a PromQL time series over a bounded window from an "
            "approved Grafana datasource. Runs a range query through platform-api with "
            "server-side query limits, workspace policy, and label sanitization. Defaults "
            "to response_mode=compact with bounded series and samples."
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
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims series/samples; full returns API payload.",
                },
                "max_series": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum metric series in compact mode.",
                },
                "max_points": {
                    "type": "integer",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum samples per series in compact mode.",
                },
            },
            "required": ["datasource_uid", "query", "start", "end", "step"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="analyze_dashboard_health",
        title="Analyze Dashboard Health",
        description=(
            "Use this when you need a read-only health summary for an allow-listed Grafana "
            "dashboard over a time window. Extracts panel PromQL, runs guarded range queries, "
            "and returns per-panel series, anomaly flags, and a concise summary. Defaults "
            "to response_mode=compact with bounded panels, series, and samples."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {
                    "type": "string",
                    "description": "Grafana dashboard UID, URL slug, or exact allow-listed title.",
                },
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
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims panel series; full returns API payload.",
                },
                "panel_limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum panels returned in compact mode.",
                },
                "max_series": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum series per panel in compact mode.",
                },
                "max_points": {
                    "type": "integer",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum samples per series in compact mode.",
                },
            },
            "required": ["dashboard_uid"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    ),
    ToolSpec(
        name="grafana_get_panel_view",
        title="Get Grafana Panel View",
        description=(
            "Use this when the user asks to view one specific allow-listed Grafana "
            "timeseries panel in ChatGPT. Loads the panel, runs guarded Prometheus range "
            "queries through platform-api, normalizes the result for an interactive chart, "
            "and returns a GrafanaPanelView structuredContent payload."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dashboard_uid": {
                    "type": "string",
                    "description": "Grafana dashboard UID, URL slug, or exact allow-listed title.",
                },
                "panel_id": {"type": "integer", "minimum": 1, "description": "Grafana panel id."},
                "start": {
                    "type": "string",
                    "default": "now-1h",
                    "description": "Window start (default now-1h).",
                },
                "end": {
                    "type": "string",
                    "default": "now",
                    "description": "Window end (default now).",
                },
                "variables": {
                    "type": "object",
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "description": "Grafana dashboard variables to apply.",
                },
                "max_points": {
                    "type": "integer",
                    "default": 300,
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum points per rendered series.",
                },
                "workspace_id": {"type": "string", "description": "Optional workspace scope."},
            },
            "required": ["dashboard_uid", "panel_id"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        meta={
            "ui": {"resourceUri": "ui://incidentflow/grafana-panel.html"},
            "openai/outputTemplate": "ui://incidentflow/grafana-panel.html",
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "Loading Grafana panel...",
            "openai/toolInvocation/invoked": "Grafana panel loaded",
        },
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_connection_health",
        title="Check Argo CD Connection",
        description=(
            "Checks the connected Argo CD integration for the current workspace and returns "
            "read-only health, authentication, RBAC, version, and application-count metadata. "
            "Access is mediated by platform-api; Argo CD tokens are never exposed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "integration_id": {
                    "type": "string",
                    "description": (
                        "Optional Argo CD integration id. Omit when only one is connected."
                    ),
                },
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_list_applications",
        title="List Argo CD Applications",
        description=(
            "Lists Argo CD applications visible to the current workspace's connected "
            "read-only integration. Supports filtering by project, namespace, destination, "
            "health, sync status, and search text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "search": {"type": "string", "description": "Case-insensitive app search text."},
                "project": {"type": "string", "description": "Argo CD project filter."},
                "namespace": {"type": "string", "description": "Destination namespace filter."},
                "destination_cluster": {
                    "type": "string",
                    "description": "Destination cluster name or server filter.",
                },
                "health_status": {"type": "string", "description": "Health status filter."},
                "sync_status": {"type": "string", "description": "Sync status filter."},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_get_application",
        title="Get Argo CD Application",
        description=(
            "Returns normalized read-only Argo CD application details, including project, "
            "destination, sources, health, sync, recent history, and last operation summary. "
            "Defaults to response_mode=compact so long histories and operation resource "
            "results stay safe for chat context; use response_mode=full for raw diagnostics."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Argo CD application name."},
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims large nested lists; full returns API payload.",
                },
                "history_limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum history entries in compact mode.",
                },
            },
            "required": ["name"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_get_application_resources",
        title="Get Argo CD Application Resources",
        description=(
            "Returns normalized resource tree metadata for an Argo CD application. Full "
            "Kubernetes manifests and Secret data are intentionally omitted. Defaults to "
            "response_mode=compact with a bounded resource list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Argo CD application name."},
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum resources returned in compact mode.",
                },
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims large resource trees; full returns API payload.",
                },
            },
            "required": ["name"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_get_sync_history",
        title="Get Argo CD Sync History",
        description=(
            "Returns recent Argo CD deployment/sync history for one application, including "
            "revision, deployment time, source summary, and initiator metadata."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Argo CD application name."},
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "required": ["name"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_get_last_operation",
        title="Get Argo CD Last Operation",
        description=(
            "Returns the latest Argo CD operation state for one application, including "
            "phase, timing, message, sync revision, and resource result summaries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Argo CD application name."},
                "integration_id": {"type": "string", "description": "Optional integration id."},
            },
            "required": ["name"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_find_recent_deployments",
        title="Find Recent Argo CD Deployments",
        description=(
            "Returns recent deployment events across visible Argo CD applications for the "
            "current workspace, with optional project and namespace filters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "project": {"type": "string", "description": "Argo CD project filter."},
                "namespace": {"type": "string", "description": "Destination namespace filter."},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
    ToolSpec(
        name="argocd_analyze_application",
        title="Analyze Argo CD Application",
        description=(
            "Builds a compact read-only health and sync analysis for one Argo CD application "
            "from application status, last operation, history, and resource tree metadata. "
            "Defaults to compact response mode to bound embedded history and operation results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Argo CD application name."},
                "integration_id": {"type": "string", "description": "Optional integration id."},
                "response_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "compact trims large nested lists; full returns API payload.",
                },
                "history_limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum history entries in compact mode.",
                },
            },
            "required": ["name"],
        },
        annotations=_read_only_annotations(),
        structured_output=True,
    ),
]


_MEMORY_WRITE_JUSTIFICATION = (
    "This tool writes a knowledge document into IncidentFlow's semantic memory layer "
    "(Qdrant vector database) so future searches can retrieve it. It does not modify "
    "Kubernetes resources, Slack messages, or any external system."
)


# ──────────────────────────────────────────────
# Typed knowledge-memory tools (runbook / rca / postmortem / knowledge / incident)
# ──────────────────────────────────────────────

_KNOWLEDGE_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}

_DRY_RUN_PROP = {
    "type": "boolean",
    "default": False,
    "description": "If true, validate and return what would be stored without writing.",
}
_TAGS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Keyword tags for filtering, e.g. ['kubernetes', 'startupProbe'].",
}

_TOOL_SPECS.extend(
    [
        ToolSpec(
            name="knowledge_upsert",
            title="Save Knowledge Document",
            description=(
                "Create or update one private workspace knowledge document. Use "
                "document_type to save general knowledge, incidents, RCAs, postmortems, "
                "or runbooks through one stable write contract. Re-saving the same id "
                "updates the existing record."
                f"{_MEMORY_WRITE_JUSTIFICATION}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_type": {
                        "type": "string",
                        "enum": ["knowledge", "incident", "rca", "postmortem", "runbook"],
                        "description": "Kind of private knowledge document to save.",
                    },
                    "title": {"type": "string"},
                    "text": {
                        "type": "string",
                        "description": "Markdown or plain text body to store.",
                    },
                    "id": {
                        "type": "string",
                        "description": (
                            "Stable document id. Required for incidents; generated from title "
                            "for other document types when omitted."
                        ),
                    },
                    "service": {"type": "string", "description": "Optional service name."},
                    "cluster": {"type": "string", "description": "Optional cluster name."},
                    "namespace": {"type": "string", "description": "Optional namespace."},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "status": {
                        "type": "string",
                        "description": "Optional status, e.g. active, draft, open, or resolved.",
                    },
                    "started_at": {"type": "string", "format": "date-time"},
                    "tags": _TAGS_PROP,
                    "dry_run": _DRY_RUN_PROP,
                },
                "required": ["document_type", "title", "text"],
            },
            annotations=_KNOWLEDGE_WRITE_ANNOTATIONS,
            structured_output=True,
        ),
    ]
)


def get_tool_specs() -> list[ToolSpec]:
    """Return all registered tool specifications."""
    return list(_TOOL_SPECS)
