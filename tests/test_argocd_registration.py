"""Smoke tests: Argo CD tools are declared in the registry and registered on the server."""

from __future__ import annotations

from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

ARGOCD_TOOLS = {
    "argocd_connection_health",
    "argocd_list_applications",
    "argocd_get_application",
    "argocd_get_application_resources",
    "argocd_get_sync_history",
    "argocd_get_last_operation",
    "argocd_find_recent_deployments",
    "argocd_analyze_application",
}


def test_registry_declares_argocd_tools() -> None:
    specs = {s.name: s for s in get_tool_specs()}
    assert ARGOCD_TOOLS <= set(specs)
    for name in ARGOCD_TOOLS:
        spec = specs[name]
        assert spec.description
        assert spec.required_integration == "argocd"
        assert spec.input_schema["type"] == "object"
        assert "workspace_id" not in spec.input_schema["properties"]
        assert spec.annotations["readOnlyHint"] is True
        assert spec.annotations["openWorldHint"] is False
        assert spec.annotations["destructiveHint"] is False
        assert spec.structured_output is True


def test_required_inputs_declared() -> None:
    specs = {s.name: s for s in get_tool_specs()}
    assert specs["argocd_connection_health"].input_schema["required"] == []
    assert specs["argocd_list_applications"].input_schema["required"] == []
    assert specs["argocd_find_recent_deployments"].input_schema["required"] == []
    assert specs["argocd_get_application"].input_schema["required"] == ["name"]
    assert specs["argocd_get_application_resources"].input_schema["required"] == ["name"]
    assert specs["argocd_get_sync_history"].input_schema["required"] == ["name"]
    assert specs["argocd_get_last_operation"].input_schema["required"] == ["name"]
    assert specs["argocd_analyze_application"].input_schema["required"] == ["name"]


async def test_server_registers_argocd_tools() -> None:
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert ARGOCD_TOOLS <= names

    for name in ARGOCD_TOOLS:
        assert tools[name].outputSchema["type"] == "object"

    assert tools["argocd_list_applications"].inputSchema["properties"]["limit"]["maximum"] == 200
    assert (
        tools["argocd_find_recent_deployments"].inputSchema["properties"]["limit"]["maximum"]
        == 200
    )
    assert tools["argocd_get_sync_history"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert tools["argocd_get_application"].inputSchema["properties"]["name"]["minLength"] == 1
    assert (
        tools["argocd_get_application"].inputSchema["properties"]["response_mode"]["enum"]
        == ["compact", "full"]
    )
    assert (
        tools["argocd_get_application"].inputSchema["properties"]["history_limit"]["maximum"]
        == 20
    )
    assert (
        tools["argocd_get_application_resources"].inputSchema["properties"]["limit"]["maximum"]
        == 200
    )
    assert (
        tools["argocd_analyze_application"].inputSchema["properties"]["history_limit"]["maximum"]
        == 20
    )
