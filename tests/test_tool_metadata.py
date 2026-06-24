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
    "k8s_agent_command",
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
    "k8s_show_namespaces",
    "k8s_show_pods",
    "k8s_show_unhealthy_pods",
    "k8s_analyze_workload",
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

        assert spec.annotations["readOnlyHint"] is True
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

        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.destructiveHint is False
