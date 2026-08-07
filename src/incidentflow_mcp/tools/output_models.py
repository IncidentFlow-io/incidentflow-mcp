"""Per-tool ``data`` payload models — the Pydantic-first source of truth.

Every operational (and meta) MCP tool maps to exactly one entry in
:data:`TOOL_OUTPUT_MODELS`. Response ``data`` JSON Schemas are generated from
these models via ``model_json_schema(mode="serialization")`` (see
``tools/contracts.py``), so there is no hand-maintained JSON-Schema drift.

Strictness policy:
- **strict** (``extra="forbid"`` → ``additionalProperties: false``): payloads we
  fully own and whose fields are stable.
- **permissive** (``extra="allow"`` → ``additionalProperties: true``): payloads
  that pass an upstream platform-api / agent body through. Known/owned fields are
  still declared and typed; unknown upstream additions are tolerated so a live
  response never fails output validation.

A registry entry may also be a raw JSON-Schema ``dict`` for the one tool
(``external_status_check``) whose response is a genuine ``oneOf`` of variants that
a single Pydantic model cannot express cleanly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Reused existing output models.
from incidentflow_mcp.tools.argocd import ArgoCDOutput
from incidentflow_mcp.tools.grafana import (
    AnalyzeOutput,
    DashboardDetailOutput,
    ExtractQueriesOutput,
    GrafanaConnectionHealthOutput,
    ListDashboardsOutput,
    PanelViewOutput,
    QueryOutput,
)
from incidentflow_mcp.tools.integration_guide import IntegrationGuideOutput
from incidentflow_mcp.tools.schemas import CorrelateAlertsOutput, IncidentSummaryOutput
from incidentflow_mcp.tools.slack_alerts import SlackAlertsOutput, SlackAlertThreadOutput

_STRICT = ConfigDict(extra="forbid")
_PERMISSIVE = ConfigDict(extra="allow")

# k8s integration lifecycle values (widened per review #5).
K8sStatus = Literal["connected", "degraded", "offline", "not_configured", "unknown"]


# ---------------------------------------------------------------------------
# Shared submodels
# ---------------------------------------------------------------------------
class ClusterConnection(BaseModel):
    """Shared-dev connection context injected by _with_integration_context."""

    model_config = _PERMISSIVE
    source: str | None = None
    cluster: str | None = None
    environment: str | None = None


class ClusterSummary(BaseModel):
    model_config = _PERMISSIVE
    cluster_id: str | None = None
    name: str | None = None
    environment: str | None = None
    connected: bool | None = None


# ---------------------------------------------------------------------------
# Meta tools
# ---------------------------------------------------------------------------
class MCPVersionImage(BaseModel):
    model_config = _STRICT
    ref: str | None = None
    digest: str | None = None
    signed: bool
    signature_verified: bool
    signature_issuer: str | None = None
    signature_identity: str | None = None


class MCPVersionTools(BaseModel):
    model_config = _STRICT
    registered: int
    operational: int
    meta: int


class MCPVersionData(BaseModel):
    """`mcp_version` — fully owned, strict.

    Version fields are named distinctly from the envelope (`current_api_version`,
    `contract_version`) so `data` never duplicates `envelope.api_version` /
    `envelope.schema_version` (review #3, #4).
    """

    model_config = _STRICT
    service: str
    service_version: str
    current_api_version: str
    contract_version: str
    supported_api_versions: list[str]
    supported_schema_versions: list[str]
    deprecated_api_versions: list[str]
    environment: str
    tag: str | None = None
    commit: str | None = None
    built_at: str | None = None
    tools: MCPVersionTools
    image: MCPVersionImage


# ---------------------------------------------------------------------------
# Kubernetes — strict (fully owned)
# ---------------------------------------------------------------------------
class K8sAgentStatusData(BaseModel):
    """`k8s_agent_status` — strict. `status` is the integration lifecycle;
    `healthy` the aggregate boolean. `offline` is a valid observed result, not an
    error, so it stays in `data` (only action-tool failures are hoisted)."""

    model_config = _STRICT
    status: K8sStatus
    healthy: bool
    checked_at: datetime
    cluster_id: str | None = None
    cluster_name: str | None = None
    environment: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    agent_status: str | None = None
    last_seen_at: str | None = None
    last_heartbeat_at: str | None = None
    # optional shared-dev context injected by _with_integration_context
    connection: ClusterConnection | None = None
    warnings: list[str] = Field(default_factory=list)


class K8sPermissionResult(BaseModel):
    model_config = _STRICT
    allowed: bool | None = None
    error_code: str | None = None
    message: str | None = None


class K8sRbacPermissions(BaseModel):
    model_config = _STRICT
    list_namespaces: K8sPermissionResult | None = None
    list_pods: K8sPermissionResult | None = None
    list_events: K8sPermissionResult | None = None
    list_deployments: K8sPermissionResult | None = None
    list_services: K8sPermissionResult | None = None
    get_logs: K8sPermissionResult | None = None


class K8sRbacCheckData(BaseModel):
    """`k8s_rbac_check` — strict."""

    model_config = _STRICT
    read_only: bool
    checked_at: datetime
    permissions: K8sRbacPermissions
    cluster_id: str | None = None


class K8sConnectionHealthData(BaseModel):
    """`k8s_connection_health` — owned wrapper; permissive on the variable
    latency/permission surface so a live probe never fails validation."""

    model_config = _PERMISSIVE
    status: K8sStatus
    healthy: bool
    checked_at: datetime
    read_only: bool | None = None
    latency_ms: float | None = None
    latency_interpretation: str | None = None
    latency_breakdown: dict[str, Any] | None = None
    namespaces_visible: int | None = None
    namespaces: list[str] = Field(default_factory=list)
    permissions: dict[str, bool | None] = Field(default_factory=dict)
    cluster_id: str | None = None
    cluster_name: str | None = None
    environment: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    agent_status: str | None = None
    last_seen_at: str | None = None
    last_heartbeat_at: str | None = None
    connection: ClusterConnection | None = None
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Kubernetes — permissive (owned wrapper, opaque agent items)
# ---------------------------------------------------------------------------
class K8sListData(BaseModel):
    """Wrapper for k8s list_pods/list_deployments/list_services/list_events."""

    model_config = _PERMISSIVE
    status: str | None = None
    summary: str | None = None
    data: dict[str, Any] | None = None
    error: dict[str, Any] | str | None = None
    warnings: list[str] = Field(default_factory=list)


class K8sOverviewData(BaseModel):
    """cluster_overview / namespace_overview — owned scalars, opaque event lists."""

    model_config = _PERMISSIVE
    status: str | None = None
    cluster_health: str | None = None
    cluster_id: str | None = None
    cluster_name: str | None = None
    namespace: str | None = None
    checked_at: datetime | None = None
    summary: str | None = None
    findings: list[Any] = Field(default_factory=list)
    recommendations: list[Any] = Field(default_factory=list)


class K8sAnalysisData(BaseModel):
    """show_unhealthy_pods / analyze_workload / describe_pod / debug_pod / get_pod."""

    model_config = _PERMISSIVE
    status: str | None = None
    summary: str | None = None
    health: str | None = None
    severity: str | None = None
    findings: list[Any] = Field(default_factory=list)
    recommendations: list[Any] = Field(default_factory=list)
    data: dict[str, Any] | None = None
    memory_context: dict[str, Any] | None = None


class K8sPassthroughData(BaseModel):
    """list_namespaces / get_rollout_status — opaque agent payload."""

    model_config = _PERMISSIVE
    status: str | None = None
    data: dict[str, Any] | None = None
    error: dict[str, Any] | str | None = None


# ---------------------------------------------------------------------------
# Argo CD (all passthrough via ArgoCDOutput) + connection-health normalization
# ---------------------------------------------------------------------------
class ArgoCDCheck(BaseModel):
    model_config = _STRICT
    name: str
    status: Literal["ok", "failed", "skipped", "warning"]
    message: str


class ArgoCDConnectionHealthData(ArgoCDOutput):
    """`argocd_connection_health` — owned `healthy` + typed `checks`, upstream tolerant."""

    model_config = _PERMISSIVE
    healthy: bool
    message: str | None = None
    checks: list[ArgoCDCheck] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------
class KnowledgeResult(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None
    type: str | None = None
    title: str | None = None
    score: float | None = None


class PublicKnowledgeSearchData(BaseModel):
    """Normalized public search response (review #9).

    Permissive until the platform-api search payload is pinned: the handler adapter
    always surfaces the normalized fields, while upstream extras are tolerated so a
    live response never fails validation.
    """

    model_config = _PERMISSIVE
    query: str | None = None
    scope: Literal["public"] | None = None
    results: list[KnowledgeResult] = Field(default_factory=list)
    total: int | None = Field(default=None, ge=0)
    next_cursor: str | None = None


class PrivateKnowledgeSearchData(BaseModel):
    model_config = _PERMISSIVE
    query: str | None = None
    scope: Literal["workspace"] | None = None
    results: list[KnowledgeResult] = Field(default_factory=list)
    total: int | None = Field(default=None, ge=0)
    next_cursor: str | None = None


class KnowledgeGetData(BaseModel):
    """`knowledge_get` — platform passthrough, known fields declared."""

    model_config = _PERMISSIVE
    found: bool | None = None
    id: str | None = None
    type: str | None = None
    title: str | None = None


class KnowledgeUpsertData(BaseModel):
    """`knowledge_upsert` — strict; two variants (stored / dry_run) unified."""

    model_config = _STRICT
    stored: bool
    type: str
    id: str | None = None
    title: str | None = None
    operation: str | None = None
    created: bool | None = None
    updated: bool | None = None
    point_id: str | None = None
    text_hash: str | None = None
    dry_run: bool | None = None
    validated: bool | None = None
    # dry-run echoes the document that *would* be written (a payload dict).
    would_write: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Incident / Slack (reuse existing models; add ad-hoc memory_context)
# ---------------------------------------------------------------------------
class IncidentSummaryData(IncidentSummaryOutput):
    model_config = _PERMISSIVE
    memory_context: dict[str, Any] | None = None


class CorrelateAlertsData(CorrelateAlertsOutput):
    model_config = _PERMISSIVE
    memory_context: dict[str, Any] | None = None


class IncidentThreadSummaryData(BaseModel):
    """`incident_thread_summary` — strict; stable owned dict."""

    model_config = _STRICT
    title: str
    status: Literal["unknown", "investigating", "mitigated"]
    summary: str
    what_engineers_said: list[str] = Field(default_factory=list)
    probable_root_cause: str | None = None
    actions_taken: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    runbooks: list[dict[str, Any]] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Meta status tools (permissive — dynamic / evolving)
# ---------------------------------------------------------------------------
class AuthStatusData(BaseModel):
    model_config = _PERMISSIVE
    authenticated: bool
    environment: str | None = None


class CapabilitiesData(BaseModel):
    model_config = _PERMISSIVE
    name: str | None = None
    total: int | None = None
    categories: list[Any] = Field(default_factory=list)


class IntegrationsStatusData(BaseModel):
    model_config = _PERMISSIVE


# ---------------------------------------------------------------------------
# external_status_check — raw oneOf schema (three variants, review #10, #11)
# ---------------------------------------------------------------------------
_ISO = {"type": "string", "format": "date-time"}
_JOB_STATUS = {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled"]}
_CHECK_STATUS = {
    "type": "string",
    "enum": ["success", "partial_success", "failed", "unknown"],
}

EXTERNAL_STATUS_CHECK_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {  # async ack / in-flight poll
            "type": "object",
            "additionalProperties": True,
            "required": ["mode", "job_id", "job_status"],
            "properties": {
                "mode": {"const": "async"},
                "job_id": {"type": "string"},
                "job_status": _JOB_STATUS,
                "poll_after_seconds": {"type": ["integer", "null"], "minimum": 1},
                "providers": {"type": "array"},
            },
        },
        {  # completed compact result
            "type": "object",
            "additionalProperties": True,
            "required": ["mode", "check_status", "providers"],
            "properties": {
                "mode": {"const": "completed"},
                "check_status": _CHECK_STATUS,
                "checked_at": {"anyOf": [_ISO, {"type": "null"}]},
                "providers": {"type": "array"},
                "errors": {"type": "array"},
            },
        },
        {  # completed full job envelope
            "type": "object",
            "additionalProperties": True,
            "required": ["mode", "job_id", "job_status"],
            "properties": {
                "mode": {"const": "completed"},
                "job_id": {"type": "string"},
                "job_status": _JOB_STATUS,
                "result": {"type": ["object", "null"]},
                "response_mode": {"type": ["string", "null"]},
            },
        },
    ]
}


# ---------------------------------------------------------------------------
# Registry: tool name -> Pydantic model OR raw schema dict
# ---------------------------------------------------------------------------
TOOL_OUTPUT_MODELS: dict[str, type[BaseModel] | dict[str, Any]] = {
    # meta
    "mcp_version": MCPVersionData,
    "incidentflow_capabilities": CapabilitiesData,
    "incidentflow_auth_status": AuthStatusData,
    "incidentflow_integrations_status": IntegrationsStatusData,
    # kubernetes
    "k8s_agent_status": K8sAgentStatusData,
    "k8s_connection_health": K8sConnectionHealthData,
    "k8s_rbac_check": K8sRbacCheckData,
    "k8s_cluster_overview": K8sOverviewData,
    "k8s_namespace_overview": K8sOverviewData,
    "k8s_show_unhealthy_pods": K8sAnalysisData,
    "k8s_analyze_workload": K8sAnalysisData,
    "k8s_describe_pod": K8sAnalysisData,
    "k8s_debug_pod": K8sAnalysisData,
    "k8s_get_pod": K8sAnalysisData,
    "k8s_list_pods": K8sListData,
    "k8s_list_deployments": K8sListData,
    "k8s_list_services": K8sListData,
    "k8s_list_events": K8sListData,
    "k8s_get_pod_logs": K8sListData,
    "k8s_list_namespaces": K8sPassthroughData,
    "k8s_get_rollout_status": K8sPassthroughData,
    # argocd
    "argocd_connection_health": ArgoCDConnectionHealthData,
    "argocd_list_applications": ArgoCDOutput,
    "argocd_get_application": ArgoCDOutput,
    "argocd_get_application_resources": ArgoCDOutput,
    "argocd_get_sync_history": ArgoCDOutput,
    "argocd_get_last_operation": ArgoCDOutput,
    "argocd_find_recent_deployments": ArgoCDOutput,
    "argocd_analyze_application": ArgoCDOutput,
    # grafana
    "grafana_connection_health": GrafanaConnectionHealthOutput,
    "grafana_list_dashboards": ListDashboardsOutput,
    "grafana_get_dashboard": DashboardDetailOutput,
    "grafana_extract_panel_queries": ExtractQueriesOutput,
    "grafana_metrics_query": QueryOutput,
    "grafana_metrics_query_range": QueryOutput,
    "analyze_dashboard_health": AnalyzeOutput,
    "grafana_get_panel_view": PanelViewOutput,
    # slack / incident
    "slack_alerts_list": SlackAlertsOutput,
    "slack_alert_thread_get": SlackAlertThreadOutput,
    "incident_thread_summary": IncidentThreadSummaryData,
    "incident_summary": IncidentSummaryData,
    "correlate_alerts": CorrelateAlertsData,
    "external_status_check": EXTERNAL_STATUS_CHECK_SCHEMA,
    # knowledge
    "public_knowledge_search": PublicKnowledgeSearchData,
    "private_knowledge_search": PrivateKnowledgeSearchData,
    "knowledge_get": KnowledgeGetData,
    "knowledge_upsert": KnowledgeUpsertData,
    "integration_guide": IntegrationGuideOutput,
}


def schema_mode_for(tool_name: str) -> Literal["strict", "permissive"]:
    """Return whether a tool's data schema forbids unknown fields."""

    entry = TOOL_OUTPUT_MODELS.get(tool_name)
    if entry is None:
        return "permissive"
    if isinstance(entry, dict):
        # raw oneOf schema: strict only if every branch forbids additions
        branches = entry.get("oneOf") or [entry]
        return (
            "strict"
            if all(b.get("additionalProperties") is False for b in branches)
            else "permissive"
        )
    extra = entry.model_config.get("extra")
    return "strict" if extra == "forbid" else "permissive"
