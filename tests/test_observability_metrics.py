"""Observability metrics coverage for HTTP/MCP middleware."""

import logging
import re

from fastapi.testclient import TestClient

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.metrics import classify_traffic, normalize_route
from incidentflow_mcp.observability.middleware import (
    MCPObservabilityMiddleware,
    _mcp_tool_event_from_body,
    _tool_metric_outcome,
)


async def _empty_asgi_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    del scope, receive, send


def test_classify_traffic_uses_operational_categories() -> None:
    assert classify_traffic("/healthz") == "health"
    assert classify_traffic("/readyz") == "health"
    assert classify_traffic("/metrics") == "metrics"
    assert classify_traffic("/mcp") == "business"
    assert classify_traffic("/install.sh") == "internal"


def test_known_well_known_routes_are_not_unmatched() -> None:
    assert normalize_route("/.well-known/oauth-protected-resource") == (
        "/.well-known/oauth-protected-resource"
    )
    assert normalize_route("/.well-known/jwks.json") == "/.well-known/jwks.json"
    assert normalize_route("/favicon.ico") == "unmatched"


def test_metrics_endpoint_exposes_observability_metrics(auth_client: TestClient) -> None:
    auth_client.get("/healthz")
    auth_client.get("/readyz")

    payload = auth_client.get("/metrics").text

    assert "http_requests_total" in payload
    assert "http_request_duration_seconds" in payload
    assert "http_requests_in_flight" in payload
    assert "mcp_sessions_active" in payload
    assert "mcp_sessions_started_total" in payload
    assert "mcp_sessions_terminated_total" in payload
    assert "mcp_session_duration_seconds" in payload
    assert "mcp_request_type_total" in payload
    assert "mcp_request_type_duration_seconds" in payload
    assert "mcp_connections_active" in payload
    assert "mcp_tool_requests_total" in payload
    assert "mcp_tool_request_duration_seconds" in payload
    assert "mcp_tool_requests_in_flight" in payload
    assert "mcp_registered_tools" in payload
    assert "mcp_tool_errors_total" in payload
    assert "mcp_integration_guard_total" in payload
    assert "mcp_auth_failures_total" in payload
    assert "mcp_auth_success_total" in payload
    assert "mcp_request_duration_seconds" in payload
    assert "tool_duration_seconds" in payload


def test_metrics_endpoint_exposes_registered_tool_inventory(auth_client: TestClient) -> None:
    payload = auth_client.get("/metrics").text

    assert (
        'mcp_registered_tools{category="meta",read_only="true",tool="mcp_version"} 1.0'
        in payload
    )
    assert (
        'mcp_registered_tools{category="kubernetes",read_only="true",tool="k8s_list_pods"} 1.0'
        in payload
    )
    assert (
        'mcp_registered_tools{category="knowledge",read_only="false",tool="knowledge_upsert"} 1.0'
        in payload
    )


def test_health_and_business_traffic_are_labeled_separately(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    auth_client.get("/healthz")
    auth_client.get("/readyz")
    auth_client.post(
        "/mcp",
        headers=valid_auth_headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    payload = auth_client.get("/metrics").text

    healthz_traffic = (
        'http_requests_total{method="GET",route="/healthz",status_code="200",traffic="health"}'
    )
    readyz_traffic = (
        'http_requests_total{method="GET",route="/readyz",status_code="200",traffic="health"}'
    )
    mcp_200 = 'http_requests_total{method="POST",route="/mcp",status_code="200",traffic="business"}'
    mcp_202 = 'http_requests_total{method="POST",route="/mcp",status_code="202",traffic="business"}'
    mcp_500 = 'http_requests_total{method="POST",route="/mcp",status_code="500",traffic="business"}'
    mcp_type_200 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="200"}'
    mcp_type_202 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="202"}'
    mcp_type_500 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="500"}'

    assert healthz_traffic in payload
    assert readyz_traffic in payload
    assert mcp_200 in payload or mcp_202 in payload or mcp_500 in payload
    assert mcp_type_200 in payload or mcp_type_202 in payload or mcp_type_500 in payload
    assert 'route="/metrics"' not in payload


def test_call_tool_request_captures_tool_labels(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    auth_client.post(
        "/mcp",
        headers=valid_auth_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "incident_summary", "arguments": {"incident_id": "INC-001"}},
        },
    )

    payload = auth_client.get("/metrics").text
    assert "mcp_tool_requests_total{" in payload
    assert re.search(r'mcp_tool_requests_total\{[^}]*tool="incident_summary"[^}]*\}', payload)
    assert re.search(r'mcp_tool_requests_total\{[^}]*method="CallToolRequest"[^}]*\}', payload)
    assert re.search(r'mcp_tool_requests_total\{[^}]*traffic_type="business"[^}]*\}', payload)
    # Clean per-tool latency metric records the bounded tool + status labels.
    assert re.search(r'tool_duration_seconds_count\{[^}]*tool="incident_summary"[^}]*\}', payload)
    assert re.search(r'tool_duration_seconds_count\{[^}]*status="(success|error)"[^}]*\}', payload)
    # Full MCP request latency is labeled by endpoint + method only.
    assert re.search(
        r'mcp_request_duration_seconds_count\{endpoint="/mcp",method="POST"\}', payload
    )


def test_call_tool_without_name_uses_unknown_tool(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    auth_client.post(
        "/mcp",
        headers=valid_auth_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}},
    )

    payload = auth_client.get("/metrics").text
    assert "mcp_tool_requests_total{" in payload
    assert 'tool="unknown"' in payload
    # The clean per-tool latency metric must never record the "unknown" placeholder.
    for line in payload.splitlines():
        if line.startswith("tool_duration_seconds"):
            assert 'tool="unknown"' not in line


def test_headerless_request_increments_inferred_session_start(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    auth_client.post(
        "/mcp",
        headers=valid_auth_headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )

    payload = auth_client.get("/metrics").text
    assert 'mcp_sessions_started_total{reason="inferred_request"}' in payload


def test_request_log_uses_structured_http_fields(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="incidentflow_mcp.observability.middleware")

    auth_client.get("/healthz", headers=valid_auth_headers)

    record = next(
        item
        for item in caplog.records
        if item.name == "incidentflow_mcp.observability.middleware"
        and item.getMessage() == "http_request_completed"
    )

    assert record.http_method == "GET"
    assert record.http_route == "/healthz"
    assert record.http_status_code == 200
    assert record.http_duration_ms >= 0
    assert record.request_id
    assert not hasattr(record, "http_status_class")
    assert not hasattr(record, "outcome")
    assert not hasattr(record, "traffic")
    assert not hasattr(record, "mcp_request_type")
    assert not hasattr(record, "session_mode")
    assert not hasattr(record, "auth_method")
    assert not hasattr(record, "tool_name")
    assert not hasattr(record, "tool")


def test_mcp_tool_log_uses_tool_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger="incidentflow_mcp.observability.middleware")
    middleware = MCPObservabilityMiddleware(
        _empty_asgi_app,
        settings=Settings(_env_file=None, redis_url="redis://test-only"),
    )
    middleware._log_mcp_tool(
        tool_name="incidentflow_auth_status",
        request_type="CallToolRequest",
        status_code=200,
        outcome="success",
        duration_ms=5.16,
        request_id="req-1",
        workspace_id="ws-test",
        tool_event=None,
    )

    record = next(
        item
        for item in caplog.records
        if item.name == "incidentflow_mcp.observability.middleware"
        and item.getMessage() == "mcp_tool_succeeded"
    )

    assert record.tool_name == "incidentflow_auth_status"
    assert record.duration_ms >= 0
    assert record.request_id
    assert not hasattr(record, "integration")
    assert not hasattr(record, "mcp_request_type")


def test_mcp_tool_rejection_log_uses_tool_outcome_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger="incidentflow_mcp.observability.middleware")
    middleware = MCPObservabilityMiddleware(
        _empty_asgi_app,
        settings=Settings(_env_file=None, redis_url="redis://test-only"),
    )
    tool_event = _mcp_tool_event_from_body(
        body=(
            b"event: message\r\n"
            b'data: {"jsonrpc":"2.0","id":43,"result":{"structuredContent":'
            b'{"ok":false,"code":"INTEGRATION_NOT_CONNECTED","integration":"argocd",'
            b'"message":"Argo CD is not connected for the current workspace."}}}\r\n\r\n'
        ),
        content_type="text/event-stream",
        tool_name="argocd_connection_health",
    )
    middleware._log_mcp_tool(
        tool_name="argocd_connection_health",
        request_type="CallToolRequest",
        status_code=200,
        outcome="success",
        duration_ms=5.12,
        request_id="req-2",
        workspace_id="ws-test",
        tool_event=tool_event,
    )

    record = next(
        item
        for item in caplog.records
        if item.name == "incidentflow_mcp.observability.middleware"
        and item.getMessage() == "mcp_tool_rejected"
    )

    assert record.tool_name == "argocd_connection_health"
    assert record.integration == "argocd"
    assert record.reason == "integration_missing"
    assert record.error_code == "INTEGRATION_NOT_CONNECTED"
    assert record.remediation == "connect_argocd_integration"
    assert record.retryable is False
    assert record.duration_ms >= 0
    assert record.request_id
    assert not hasattr(record, "error_type")


def test_mcp_tool_metric_outcome_distinguishes_rejected_from_http_success() -> None:
    assert (
        _tool_metric_outcome(
            http_outcome="success",
            tool_event={"outcome": "rejected", "reason": "integration_missing"},
        )
        == "rejected"
    )
    assert (
        _tool_metric_outcome(
            http_outcome="success",
            tool_event={"outcome": "failed", "error_code": "TOOL_ERROR"},
        )
        == "error"
    )
    assert _tool_metric_outcome(http_outcome="success", tool_event=None) == "success"


def test_http_and_mcp_error_logs_include_diagnostics(
    auth_client: TestClient,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="incidentflow_mcp.observability.middleware")

    auth_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "slack_post_update"},
        },
    )

    http_record = next(
        item
        for item in caplog.records
        if item.name == "incidentflow_mcp.observability.middleware"
        and item.getMessage() == "http_request_failed"
    )
    tool_record = next(
        item
        for item in caplog.records
        if item.name == "incidentflow_mcp.observability.middleware"
        and item.getMessage() == "mcp_tool_failed"
    )

    assert http_record.error_code == "http_401"
    assert http_record.error_type == "HTTPClientError"
    assert http_record.log_message == "HTTP request failed with status 401"
    assert http_record.request_id
    assert tool_record.tool_name == "slack_post_update"
    assert tool_record.integration == "slack"
    assert tool_record.error_code == "http_401"
    assert tool_record.error_type == "MCPToolTransportError"
    assert tool_record.log_message == "MCP tool request failed with HTTP status 401"
    assert tool_record.request_id


def test_request_with_session_header_tracks_session_lifecycle(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    headers = {**valid_auth_headers, "mcp-session-id": "sess-abc"}
    auth_client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
    )

    payload = auth_client.get("/metrics").text
    assert 'mcp_sessions_started_total{reason="header"}' in payload
    assert re.search(r"mcp_sessions_active\s+[1-9]", payload) is not None


def test_health_requests_do_not_pollute_tool_metrics(auth_client: TestClient) -> None:
    auth_client.get("/healthz")
    auth_client.get("/readyz")
    payload = auth_client.get("/metrics").text
    for line in payload.splitlines():
        if line.startswith("mcp_tool_requests_total{"):
            assert 'traffic_type="health"' not in line


def test_in_flight_gauges_return_to_zero_on_mcp_error(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
) -> None:
    auth_client.post(
        "/mcp",
        headers=valid_auth_headers,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "incident_summary"},
        },
    )

    payload = auth_client.get("/metrics").text
    assert re.search(r'mcp_connections_active\{[^}]*traffic_type="business"[^}]*\}\s+0\.0', payload)
    assert re.search(
        r'mcp_tool_requests_in_flight\{[^}]*tool="incident_summary"[^}]*\}\s+0\.0',
        payload,
    )


def test_tool_duration_and_error_metrics_recorded_on_error(
    auth_client: TestClient,
) -> None:
    # Missing auth guarantees an error path while still exercising middleware parsing.
    auth_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "incident_summary"},
        },
    )

    payload = auth_client.get("/metrics").text
    assert 'mcp_auth_failures_total{reason="missing_header"}' in payload
    assert re.search(
        r'mcp_tool_request_duration_seconds_count\{[^}]*method="CallToolRequest"[^}]*outcome="error"[^}]*\}',
        payload,
    )
    assert "mcp_tool_errors_total{" in payload
