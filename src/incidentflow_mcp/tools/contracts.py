"""Versioned response contracts for IncidentFlow MCP tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from incidentflow_mcp.tools.registry import ToolSpec

TOOL_SCHEMA_VERSION = "v1"
TOOL_SCHEMA_BASE_URL = "https://incidentflow.io/schemas/tools"
RESERVED_CONTRACT_KEYS = frozenset({"schemaVersion", "schemaId", "warnings"})


class ToolError(BaseModel):
    """Common structured error payload returned by MCP tools."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    http_status: int | None = None
    details: Any | None = None


class ToolContractEnvelope(BaseModel):
    """Common v1 envelope shared by all IncidentFlow MCP tool responses."""

    model_config = ConfigDict(extra="allow")

    schemaVersion: Literal["v1"] = TOOL_SCHEMA_VERSION
    schemaId: str
    ok: bool | None = None
    status: str | None = None
    source: dict[str, Any] | str | None = None
    truncated: bool | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ToolError | dict[str, Any] | str | None = None


class ArgoCDOperationResourceResult(BaseModel):
    """One resource result from an Argo CD sync operation."""

    model_config = ConfigDict(extra="allow")

    group: str | None = None
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None
    status: str | None = None
    message: str | None = None


class ArgoCDLastOperation(BaseModel):
    """Latest Argo CD operation summary."""

    model_config = ConfigDict(extra="allow")

    phase: str | None = None
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    sync_revision: str | None = None
    resource_results: list[ArgoCDOperationResourceResult] = Field(default_factory=list)


class ArgoCDLastOperationResponse(ToolContractEnvelope):
    """Strict v1 payload contract for argocd_get_last_operation."""

    model_config = ConfigDict(extra="forbid")

    schemaId: Literal["argocd.get-last-operation"] = "argocd.get-last-operation"
    ok: bool
    application_name: str
    status: Literal["ok", "no_operation_found", "permission_denied", "not_found", "failed"]
    operation: ArgoCDLastOperation | None = None


_STRICT_RESPONSE_MODELS: dict[str, type[ToolContractEnvelope]] = {
    "argocd_get_last_operation": ArgoCDLastOperationResponse,
}


def schema_id_for_tool(tool_name: str) -> str:
    """Return the stable schema id for one MCP tool."""

    if tool_name.startswith("argocd_"):
        return f"argocd.{_slug(tool_name.removeprefix('argocd_'))}"
    if tool_name.startswith("grafana_"):
        return f"grafana.{_slug(tool_name.removeprefix('grafana_'))}"
    if tool_name == "analyze_dashboard_health":
        return "grafana.analyze-dashboard-health"
    if tool_name.startswith("k8s_"):
        return f"kubernetes.{_slug(tool_name.removeprefix('k8s_'))}"
    if tool_name.startswith("slack_"):
        return f"slack.{_slug(tool_name.removeprefix('slack_'))}"
    if tool_name.startswith("knowledge_"):
        return f"knowledge.{_slug(tool_name.removeprefix('knowledge_'))}"
    if tool_name.endswith("_knowledge_search"):
        return f"knowledge.{_slug(tool_name)}"
    if tool_name in {"incident_summary", "correlate_alerts", "incident_thread_summary"}:
        return f"incident.{_slug(tool_name)}"
    if tool_name.startswith("incidentflow_") or tool_name == "mcp_version":
        return f"platform.{_slug(tool_name)}"
    return f"tool.{_slug(tool_name)}"


def apply_tool_contract(payload: Any, *, tool_name: str) -> Any:
    """Stamp a tool response with the global v1 contract fields.

    The v1 contract is intentionally additive: existing top-level payload fields
    remain unchanged so current clients can keep reading them.
    """

    if not isinstance(payload, dict):
        return payload
    schema_id = schema_id_for_tool(tool_name)
    stamped = dict(payload)
    stamped.setdefault("schemaVersion", TOOL_SCHEMA_VERSION)
    stamped.setdefault("schemaId", schema_id)
    stamped.setdefault("warnings", [])
    return stamped


def schema_url(schema_name: str) -> str:
    """Return the canonical URL for one generated tool schema file."""

    return f"{TOOL_SCHEMA_BASE_URL}/{schema_name}"


def tool_response_model(tool_name: str) -> type[ToolContractEnvelope]:
    """Build the Pydantic response model for one registered MCP tool."""

    if tool_name in _STRICT_RESPONSE_MODELS:
        return _STRICT_RESPONSE_MODELS[tool_name]
    schema_id = schema_id_for_tool(tool_name)
    return create_model(
        f"{_pascal(tool_name)}Response",
        __base__=ToolContractEnvelope,
        schemaId=(Literal[schema_id], Field(default=schema_id)),
    )


def tool_response_models(specs: list[ToolSpec]) -> dict[str, type[ToolContractEnvelope]]:
    """Return one response model per registered MCP tool."""

    return {spec.name: tool_response_model(spec.name) for spec in specs}


def export_tool_schemas(specs: list[ToolSpec], output_dir: Path) -> list[Path]:
    """Generate JSON Schema files for the common envelope and every tool."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    envelope_path = output_dir / "tool-envelope.v1.schema.json"
    envelope_schema = ToolContractEnvelope.model_json_schema()
    envelope_schema["$id"] = schema_url("tool-envelope.v1.schema.json")
    envelope_path.write_text(
        json.dumps(envelope_schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append(envelope_path)

    for spec in sorted(specs, key=lambda item: item.name):
        model = tool_response_model(spec.name)
        schema_name = f"{schema_id_for_tool(spec.name)}.v1.schema.json"
        path = output_dir / schema_name
        schema = model.model_json_schema()
        schema["$id"] = schema_url(schema_name)
        path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    return written


def _slug(value: str) -> str:
    return value.replace("_", "-")


def _pascal(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_") if part)
