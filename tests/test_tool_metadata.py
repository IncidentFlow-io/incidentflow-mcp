import json
from pathlib import Path

import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

EXPECTED_TOOL_NAMES = {
    "incidentflow_capabilities",
    "incidentflow_version",
    "incident_summary",
    "correlate_alerts",
    "external_status_check",
    "slack_alerts_list",
    "slack_alert_thread_get",
    "incident_thread_summary",
    "k8s_connection_health",
    "k8s_cluster_overview",
    "k8s_namespace_overview",
    "k8s_rbac_check",
    "k8s_agent_status",
    "k8s_list_namespaces",
    "k8s_list_pods",
    "k8s_get_pod",
    "k8s_get_pod_logs",
    "k8s_list_events",
    "k8s_list_deployments",
    "k8s_list_services",
    "k8s_get_rollout_status",
    "k8s_show_unhealthy_pods",
    "k8s_analyze_workload",
    "k8s_describe_pod",
    "k8s_debug_pod",
    "grafana_list_dashboards",
    "grafana_get_dashboard",
    "grafana_extract_panel_queries",
    "grafana_metrics_query",
    "grafana_metrics_query_range",
    "analyze_dashboard_health",
    "grafana_get_panel_view",
    "memory_search_similar_incidents",
    "memory_get_service_context",
    "memory_find_runbook",
    # Typed knowledge-memory tools (Phase 6)
    "memory_upsert_runbook",
    "memory_upsert_rca",
    "memory_upsert_postmortem",
    "memory_upsert_knowledge",
    "memory_upsert_incident",
    "memory_find_rca",
    "memory_find_knowledge",
}

# Write tools legitimately set readOnlyHint=False; everything else must be read-only.
WRITE_TOOL_NAMES = {
    "memory_upsert_runbook",
    "memory_upsert_rca",
    "memory_upsert_postmortem",
    "memory_upsert_knowledge",
    "memory_upsert_incident",
}

REQUIRED_BOOLEAN_ANNOTATIONS = {
    "readOnlyHint",
    "openWorldHint",
    "destructiveHint",
}

REQUIRED_SUBMISSION_JUSTIFICATIONS = {
    "read_only_justification",
    "open_world_justification",
    "destructive_justification",
}

EXPECTED_CAPABILITY_CATEGORY_TOTALS = {
    "kubernetes": 17,
    "grafana_prometheus": 7,
    "slack_incidents": 6,
    "semantic_memory_read": 5,
    "semantic_memory_write": 5,
}


def _load_submission_tools() -> dict:
    submission_path = Path(__file__).resolve().parents[1] / "chatgpt-app-submission.json"
    payload = json.loads(submission_path.read_text())
    return payload["tools"]


def test_all_registry_tools_have_submission_metadata() -> None:
    specs = get_tool_specs()

    assert {spec.name for spec in specs} == EXPECTED_TOOL_NAMES

    for spec in specs:
        assert spec.title.strip(), f"{spec.name} is missing a title"
        assert spec.description.strip(), f"{spec.name} is missing a description"
        assert spec.input_schema.get("type") == "object", f"{spec.name} is missing inputSchema"

        for annotation_name in REQUIRED_BOOLEAN_ANNOTATIONS:
            value = spec.annotations.get(annotation_name)
            assert isinstance(value, bool), f"{spec.name} {annotation_name} must be a boolean"

        if spec.name not in WRITE_TOOL_NAMES:
            assert spec.annotations["readOnlyHint"] is True, f"{spec.name} should be read-only"
        assert spec.annotations["openWorldHint"] is False
        assert spec.annotations["destructiveHint"] is False


def test_chatgpt_app_submission_tools_match_registry() -> None:
    specs = {spec.name: spec for spec in get_tool_specs()}
    submission_tools = _load_submission_tools()

    assert set(submission_tools) == EXPECTED_TOOL_NAMES
    assert set(submission_tools) == set(specs)

    for name, submission_tool in submission_tools.items():
        annotations = submission_tool["annotations"]
        for annotation_name in REQUIRED_BOOLEAN_ANNOTATIONS:
            assert annotations[annotation_name] == specs[name].annotations[annotation_name]

        justifications = submission_tool["justifications"]
        for justification_name in REQUIRED_SUBMISSION_JUSTIFICATIONS:
            assert justifications[justification_name].strip(), (
                f"{name} is missing {justification_name}"
            )


@pytest.mark.asyncio
async def test_fastmcp_tools_publish_submission_metadata() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES

    for tool in tools:
        assert tool.title and tool.title.strip(), f"{tool.name} is missing a title"
        assert tool.description and tool.description.strip(), (
            f"{tool.name} is missing a description"
        )
        assert tool.inputSchema.get("type") == "object", f"{tool.name} is missing inputSchema"
        assert tool.annotations is not None, f"{tool.name} is missing annotations"

        for annotation_name in REQUIRED_BOOLEAN_ANNOTATIONS:
            value = getattr(tool.annotations, annotation_name)
            assert isinstance(value, bool), f"{tool.name} {annotation_name} must be a boolean"

        if tool.name not in WRITE_TOOL_NAMES:
            assert tool.annotations.readOnlyHint is True, f"{tool.name} should be read-only"
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.destructiveHint is False


@pytest.mark.asyncio
async def test_incidentflow_capabilities_returns_canonical_inventory() -> None:
    mcp = create_mcp_server()
    tool_manager = mcp._tool_manager
    result = await tool_manager.call_tool("incidentflow_capabilities", {})
    payload = json.loads(result)

    operational_names = EXPECTED_TOOL_NAMES - {"incidentflow_capabilities", "incidentflow_version"}
    assert payload["total"] == 39
    assert payload["total"] == len(operational_names)
    assert payload["read_only"] == 35
    assert payload["write_memory_only"] == 5
    assert "canonical" in payload["summary"]
    assert "authoritative runtime tool list" in payload["summary"]
    assert any("cached docs" in note for note in payload["notes"])

    categories = {category["id"]: category for category in payload["categories"]}
    assert set(categories) == set(EXPECTED_CAPABILITY_CATEGORY_TOTALS)
    for category_id, expected_total in EXPECTED_CAPABILITY_CATEGORY_TOTALS.items():
        assert categories[category_id]["total"] == expected_total

    returned_names = {
        tool["canonical_name"]
        for category in payload["categories"]
        for tool in category["tools"]
    }
    assert returned_names == operational_names
    assert "incidentflow_capabilities" not in returned_names
    assert "incidentflow_version" not in returned_names

    memory_write = categories["semantic_memory_write"]["tools"]
    assert all(tool["write_memory_only"] for tool in memory_write)
    assert all(tool["read_only"] is False for tool in memory_write)


@pytest.mark.asyncio
async def test_incidentflow_version_returns_build_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            environment="production",
            mcp_build_service="incidentflow-mcp",
            mcp_build_version="dev-v1.0.0",
            mcp_build_tag="dev-v1.0.0",
            mcp_build_commit="8b2e7f1",
            mcp_build_built_at="2026-07-13T12:40:18Z",
            mcp_build_environment="dev",
            redis_url="redis://test-only",
        ),
    )
    mcp = create_mcp_server()
    tool_manager = mcp._tool_manager
    result = await tool_manager.call_tool("incidentflow_version", {})
    payload = json.loads(result)

    assert payload["service"] == "incidentflow-mcp"
    assert payload["version"] == "1.0.0"
    assert payload["tag"] == "dev-v1.0.0"
    assert payload["commit"] == "8b2e7f1"
    assert payload["built_at"] == "2026-07-13T12:40:18Z"
    assert payload["environment"] == "dev"
    assert payload["tools"] == {
        "registered": len(EXPECTED_TOOL_NAMES),
        "operational": 39,
        "meta": 2,
    }
    assert "HTTP-based MCP server" in payload["description"]


@pytest.mark.asyncio
async def test_incidentflow_version_normalizes_prod_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            environment="production",
            mcp_build_version="v1.0.0",
            mcp_build_tag="v1.0.0",
            redis_url="redis://test-only",
        ),
    )
    mcp = create_mcp_server()
    tool_manager = mcp._tool_manager
    result = await tool_manager.call_tool("incidentflow_version", {})
    payload = json.loads(result)

    assert payload["version"] == "1.0.0"
    assert payload["environment"] == "prod"


@pytest.mark.asyncio
async def test_submission_risky_tool_inputs_are_structured() -> None:
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    correlate_schema = tools["correlate_alerts"].inputSchema
    alerts_field = correlate_schema["properties"]["alerts"]
    alerts_types = alerts_field.get("anyOf", [alerts_field])
    array_type = next((t for t in alerts_types if t.get("type") == "array"), None)
    assert array_type is not None, "alerts field must have an array type variant"
    assert array_type["items"]["$ref"] == "#/$defs/Alert"

    thread_schema = tools["incident_thread_summary"].inputSchema
    alert_context = thread_schema["properties"]["alert_context"]["anyOf"][0]
    assert alert_context["$ref"] == "#/$defs/IncidentThreadAlertContext"

    pods_schema = tools["k8s_list_pods"].inputSchema
    assert "namespace" in pods_schema["properties"]

    logs_schema = tools["k8s_get_pod_logs"].inputSchema
    assert "namespace" in logs_schema["required"]
    assert "pod" in logs_schema["required"]

    workload_schema = tools["k8s_analyze_workload"].inputSchema
    assert workload_schema["properties"]["workload"]["type"] == "string"
    assert workload_schema["properties"]["namespace"]["type"] == "string"
    assert workload_schema["required"] == ["workload", "namespace"]


@pytest.mark.asyncio
async def test_grafana_panel_view_publishes_apps_sdk_metadata() -> None:
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    panel_tool = tools["grafana_get_panel_view"]

    assert panel_tool.meta["openai/outputTemplate"] == "ui://incidentflow/grafana-panel.html"
    assert panel_tool.meta["ui"]["resourceUri"] == "ui://incidentflow/grafana-panel.html"
