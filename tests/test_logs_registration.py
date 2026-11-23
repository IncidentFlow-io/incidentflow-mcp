"""Vendor-neutral Logs tool contract smoke tests."""

import pytest

from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.registry import get_tool_specs

NAMES = {"logs_list_sources", "logs_connection_health", "logs_query", "logs_get_context"}


def test_logs_registry_is_read_only_and_source_bound():
    specs = {spec.name: spec for spec in get_tool_specs() if spec.name in NAMES}
    assert set(specs) == NAMES
    for spec in specs.values():
        assert spec.annotations["readOnlyHint"] is True
        assert spec.annotations["destructiveHint"] is False
        assert "read-only" in spec.description.lower()
    assert specs["logs_query"].input_schema["required"] == ["source_id", "time_range"]
    assert specs["logs_query"].input_schema["properties"]["limit"]["maximum"] == 500


@pytest.mark.asyncio
async def test_logs_tools_registered():
    server = create_mcp_server()
    names = {tool.name for tool in await server.list_tools()}
    assert NAMES <= names
