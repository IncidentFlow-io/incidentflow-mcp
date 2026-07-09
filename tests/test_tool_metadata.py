import pytest

from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

EXPECTED_TOOL_NAMES = {
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
