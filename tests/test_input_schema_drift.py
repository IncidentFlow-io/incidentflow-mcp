"""FastMCP publishes inputSchema from handler signatures, not from the registry.

This guard compares the registry's declared input constraints with the schema
FastMCP actually publishes, so a bound documented in ``registry.py`` cannot
silently be missing from what the model sees.
"""

from __future__ import annotations

from typing import Any

from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

_CONSTRAINT_KEYS = ("enum", "minimum", "maximum", "minLength", "maxLength")

# Known, reviewed drift. Each entry is (tool, property, constraint). Remove an
# entry when its handler signature publishes the constraint. Values here are
# normalized case-insensitively or by alias in code, or are domain enums that
# still need alignment with platform-api, so they are not tightened yet.
KNOWN_DRIFT = {
    ("incidentflow_capabilities", "response_mode", "enum"),
    ("integration_guide", "problem", "maxLength"),
    ("public_knowledge_search", "document_type", "enum"),
    ("public_knowledge_search", "integration", "enum"),
    ("private_knowledge_search", "document_type", "enum"),
    ("knowledge_get", "id_type", "enum"),
    ("knowledge_get", "document_type", "enum"),
    ("knowledge_get", "response_mode", "enum"),
    ("incident_summary", "execution_mode", "enum"),
    ("correlate_alerts", "execution_mode", "enum"),
    ("external_status_check", "days_back", "minimum"),
    ("external_status_check", "days_back", "maximum"),
    ("external_status_check", "execution_mode", "enum"),
    ("external_status_check", "response_mode", "enum"),
    ("slack_alerts_list", "limit", "minimum"),
    ("slack_alerts_list", "limit", "maximum"),
    ("slack_alerts_list", "thread_mode", "enum"),
    ("slack_alerts_list", "max_thread_replies", "minimum"),
    ("slack_alerts_list", "max_thread_replies", "maximum"),
    ("slack_alert_thread_get", "max_replies", "minimum"),
    ("slack_alert_thread_get", "max_replies", "maximum"),
    ("k8s_get_pod_logs", "level", "enum"),
    ("k8s_get_pod_logs", "since_minutes", "minimum"),
    ("grafana_get_panel_view", "panel_id", "minimum"),
    ("knowledge_upsert", "document_type", "enum"),
    ("knowledge_upsert", "severity", "enum"),
}


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return _resolve(defs[schema["$ref"].split("/")[-1]], defs)
    for option in schema.get("anyOf", []):
        if option.get("type") != "null":
            return _resolve(option, defs)
    return schema


async def _drift() -> tuple[set[tuple[str, str, str]], list[str]]:
    published = {tool.name: tool.inputSchema for tool in await create_mcp_server().list_tools()}
    drift: set[tuple[str, str, str]] = set()
    structural: list[str] = []
    for spec in get_tool_specs():
        schema = published[spec.name]
        defs = schema.get("$defs", {})
        declared = spec.input_schema.get("properties", {})
        actual = schema.get("properties", {})
        if set(declared) != set(actual):
            structural.append(f"{spec.name}: properties differ {set(declared) ^ set(actual)}")
        if sorted(spec.input_schema.get("required", [])) != sorted(schema.get("required", [])):
            structural.append(f"{spec.name}: required differs")
        for name, constraints in declared.items():
            if name not in actual:
                continue
            resolved = _resolve(actual[name], defs)
            for key in _CONSTRAINT_KEYS:
                if key in constraints and constraints[key] != resolved.get(key):
                    drift.add((spec.name, name, key))
    return drift, structural


async def test_published_input_schemas_match_registry_properties() -> None:
    _, structural = await _drift()
    assert structural == []


async def test_no_new_input_constraint_drift() -> None:
    drift, _ = await _drift()
    assert drift - KNOWN_DRIFT == set(), "registry constraint missing from published schema"
    assert KNOWN_DRIFT - drift == set(), "drift fixed: remove it from KNOWN_DRIFT"


async def test_kubernetes_timeout_is_bounded_in_published_schema() -> None:
    tools = {tool.name: tool.inputSchema for tool in await create_mcp_server().list_tools()}
    bounded = [
        name
        for name, schema in tools.items()
        if "timeout_seconds" in schema.get("properties", {})
        and schema["properties"]["timeout_seconds"].get("maximum") == 60
    ]
    assert len(bounded) == 17
