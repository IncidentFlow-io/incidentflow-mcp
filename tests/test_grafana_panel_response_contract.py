"""Regression coverage for the Grafana panel-view MCP response shape."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from incidentflow_mcp.mcp.compatibility.fastmcp_contracts import run_tool_with_structured_errors
from incidentflow_mcp.tools.contracts import build_output_schema

PANEL_VIEW = {
    "version": "1",
    "panel": {"id": 5, "title": "Request rate", "type": "timeseries"},
    "dashboard": {"uid": "platform", "title": "Platform"},
    "source": {"type": "grafana", "datasourceUid": "prom"},
    "visualization": {"type": "line", "stacked": False, "showLegend": True, "showTooltip": True},
    "timeRange": {"from": 1000, "to": 2000},
    "variables": {},
    "series": [],
    "data": [],
    "annotations": [],
    "links": {"grafana": "https://grafana.test/d/platform?viewPanel=5"},
    "warnings": [],
}


@pytest.mark.asyncio
async def test_panel_view_response_wraps_the_payload_once() -> None:
    class Metadata:
        async def call_fn_with_arg_validation(self, *args: object, **kwargs: object) -> object:
            return PANEL_VIEW

    class Tool:
        name = "grafana_get_panel_view"
        fn_metadata = Metadata()
        fn = object()
        is_async = True
        context_kwarg = None
        _if_output_validator = Draft202012Validator(build_output_schema(name))

    response = await run_tool_with_structured_errors(Tool(), {})

    assert response["status"] == "success"
    assert response["data"]["version"] == "1"
    assert response["data"]["panel"]["id"] == 5
    assert "structuredContent" not in response["data"]
