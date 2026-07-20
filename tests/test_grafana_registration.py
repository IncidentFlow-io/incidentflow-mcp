"""Smoke tests: Grafana tools are declared in the registry and registered on the server."""

from __future__ import annotations

from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

GRAFANA_TOOLS = {
    "grafana_list_dashboards",
    "grafana_get_dashboard",
    "grafana_extract_panel_queries",
    "grafana_metrics_query",
    "grafana_metrics_query_range",
    "analyze_dashboard_health",
    "grafana_get_panel_view",
}


def test_registry_declares_grafana_tools() -> None:
    specs = {s.name: s for s in get_tool_specs()}
    assert GRAFANA_TOOLS <= set(specs)
    for name in GRAFANA_TOOLS:
        spec = specs[name]
        assert spec.description
        assert spec.input_schema["type"] == "object"
        assert spec.annotations["readOnlyHint"] is True
        assert spec.annotations["openWorldHint"] is False


def test_required_inputs_declared() -> None:
    specs = {s.name: s for s in get_tool_specs()}
    assert specs["grafana_get_dashboard"].input_schema["required"] == ["dashboard_uid"]
    assert specs["grafana_metrics_query_range"].input_schema["required"] == [
        "datasource_uid",
        "query",
        "start",
        "end",
        "step",
    ]
    assert specs["grafana_get_panel_view"].input_schema["required"] == [
        "dashboard_uid",
        "panel_id",
    ]
    assert specs["grafana_list_dashboards"].input_schema["required"] == []


async def test_server_registers_grafana_tools() -> None:
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert GRAFANA_TOOLS <= names

    for name in GRAFANA_TOOLS:
        assert tools[name].outputSchema["type"] == "object"

    assert tools["grafana_get_dashboard"].inputSchema["properties"]["response_mode"]["enum"] == [
        "compact",
        "full",
    ]
    assert tools["grafana_get_dashboard"].inputSchema["properties"]["panel_limit"]["maximum"] == 100
    assert (
        tools["grafana_metrics_query"].inputSchema["properties"]["response_mode"]["default"]
        == "compact"
    )
    assert tools["grafana_metrics_query"].inputSchema["properties"]["max_series"]["maximum"] == 100
    assert tools["grafana_metrics_query"].inputSchema["properties"]["max_points"]["maximum"] == 1000
    assert (
        tools["grafana_metrics_query_range"].inputSchema["properties"]["max_points"]["maximum"]
        == 1000
    )
    assert (
        tools["analyze_dashboard_health"].inputSchema["properties"]["panel_limit"]["maximum"] == 50
    )
