"""Guard: every operational tool has a per-tool data schema (no silent fallback).

Fails when a new tool is added without registering an output model, or when a
tool's strictness regresses. This is the coverage gate the review asked for so
generic-fallback schemas can never hide missing contracts.
"""

from __future__ import annotations

import jsonschema

from incidentflow_mcp.tools.contracts import (
    all_schema_ids,
    build_output_schema,
    get_schema,
    schema_mode,
)
from incidentflow_mcp.tools.output_models import TOOL_OUTPUT_MODELS
from incidentflow_mcp.tools.registry import get_tool_specs

_ENVELOPE_KEYS = {
    "api_version",
    "schema_version",
    "schema_id",
    "status",
    "request_id",
    "data",
    "error",
    "meta",
}

# Meta tools are excluded from operational coverage accounting.
_META_TOOLS = {
    "incidentflow_capabilities",
    "mcp_version",
    "incidentflow_auth_status",
    "incidentflow_integrations_status",
}

# Operational tools whose data schema is intentionally strict (extra="forbid").
# Adding/removing a tool here is a deliberate contract decision, not an accident.
EXPECTED_STRICT_TOOLS = {
    "k8s_agent_status",
    "k8s_rbac_check",
    "knowledge_upsert",
    "incident_thread_summary",
}


def _operational_tools() -> set[str]:
    return {spec.name for spec in get_tool_specs()} - _META_TOOLS


def test_every_operational_tool_has_a_registered_model() -> None:
    """100% coverage: no operational tool may rely on the generic fallback."""
    missing = _operational_tools() - set(TOOL_OUTPUT_MODELS)
    assert not missing, f"Operational tools without a registered output model: {sorted(missing)}"


def test_every_meta_tool_has_a_registered_model() -> None:
    missing = _META_TOOLS - set(TOOL_OUTPUT_MODELS)
    assert not missing, f"Meta tools without a registered output model: {sorted(missing)}"


def test_registry_has_no_unknown_tools() -> None:
    known = {spec.name for spec in get_tool_specs()}
    unknown = set(TOOL_OUTPUT_MODELS) - known
    assert not unknown, f"Registry references tools not in the tool registry: {sorted(unknown)}"


def test_strict_tool_set_is_locked() -> None:
    """Catch both strict→permissive regressions and unannounced new strict tools."""
    strict = {name for name in _operational_tools() if schema_mode(name) == "strict"}
    assert strict == EXPECTED_STRICT_TOOLS, (
        f"Strict operational tools changed. expected={sorted(EXPECTED_STRICT_TOOLS)} "
        f"actual={sorted(strict)}"
    )


def test_every_tool_output_schema_is_valid_and_enveloped() -> None:
    """Every published output schema is Draft 2020-12 valid and requires the 8 keys."""
    for name in TOOL_OUTPUT_MODELS:
        schema = build_output_schema(name)
        jsonschema.Draft202012Validator.check_schema(schema)
        required = set(schema.get("required", []))
        assert _ENVELOPE_KEYS.issubset(required), (
            f"{name}: missing {sorted(_ENVELOPE_KEYS - required)}"
        )
        assert (
            schema["properties"]["schema_id"]["const"]
            == f"incidentflow.{name.replace('_', '-')}.response"
        )


def test_schema_catalog_resolves_every_id() -> None:
    for sid in all_schema_ids():
        assert get_schema(sid) is not None, f"catalog id has no schema: {sid}"
