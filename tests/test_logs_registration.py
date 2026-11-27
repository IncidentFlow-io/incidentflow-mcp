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


def _entries(n):
    return [{"timestamp": "2026-11-27T19:00:00Z", "line": f"l{i}"} for i in range(n)]


LIMITS = {"max_lines": 100, "max_bytes": 65536, "max_lookback_seconds": 900}


def test_logs_query_truncation_on_line_limit_has_no_cursor_and_explains_reason():
    from incidentflow_mcp.mcp.registration.logs import _with_truncation

    result = _with_truncation(
        {"entries": _entries(3), "next_cursor": None, "truncated": True, "applied_limits": LIMITS},
        3,
    )
    assert "next_cursor" not in result
    assert result["truncation"]["reason"] == "line_limit"
    assert result["truncation"]["returned"] == 3
    assert "up to the source maximum of 100" in result["truncation"]["detail"]
    assert "Pagination is not supported" in result["truncation"]["hint"]


def test_logs_query_truncation_on_byte_budget():
    from incidentflow_mcp.mcp.registration.logs import _with_truncation

    result = _with_truncation(
        {"entries": _entries(7), "truncated": True, "applied_limits": LIMITS}, 100
    )
    assert result["truncation"]["reason"] == "byte_limit"
    assert "65536" in result["truncation"]["detail"]


def test_logs_untruncated_and_error_results():
    from incidentflow_mcp.mcp.registration.logs import _with_truncation

    ok = _with_truncation(
        {"entries": _entries(1), "next_cursor": None, "truncated": False, "applied_limits": {}},
        10,
    )
    assert ok["truncation"] is None and "next_cursor" not in ok
    error = {"__tool_error__": {"code": "RATE_LIMITED"}}
    assert _with_truncation(error, 10) is error
    assert _with_truncation({"entries": [], "truncated": True}, None)["truncation"]["reason"] == (
        "result_limit"
    )


def test_logs_query_output_schema_has_no_pagination_hint():
    from incidentflow_mcp.tools.output_models import LogsQueryOutput

    props = LogsQueryOutput.model_json_schema()["properties"]
    assert "next_cursor" not in props
    assert "truncation" in props
    specs = {spec.name: spec for spec in get_tool_specs()}
    assert "cursor" not in specs["logs_query"].input_schema["properties"]
    assert "no pagination" in specs["logs_query"].description
