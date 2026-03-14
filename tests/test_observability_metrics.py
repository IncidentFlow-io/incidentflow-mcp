"""Observability metrics coverage for HTTP/MCP middleware."""

import re

from fastapi.testclient import TestClient


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
    assert "mcp_tool_errors_total" in payload


def test_probe_and_business_traffic_are_labeled_separately(
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

    healthz_probe = (
        'http_requests_total{method="GET",route="/healthz",status_code="200",traffic="probe"}'
    )
    readyz_probe = (
        'http_requests_total{method="GET",route="/readyz",status_code="200",traffic="probe"}'
    )
    mcp_200 = 'http_requests_total{method="POST",route="/mcp",status_code="200",traffic="business"}'
    mcp_202 = 'http_requests_total{method="POST",route="/mcp",status_code="202",traffic="business"}'
    mcp_500 = 'http_requests_total{method="POST",route="/mcp",status_code="500",traffic="business"}'
    mcp_type_200 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="200"}'
    mcp_type_202 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="202"}'
    mcp_type_500 = 'mcp_request_type_total{request_type="ListToolsRequest",status_code="500"}'

    assert healthz_probe in payload
    assert readyz_probe in payload
    assert (
        mcp_200 in payload
        or mcp_202 in payload
        or mcp_500 in payload
    )
    assert (
        mcp_type_200 in payload
        or mcp_type_202 in payload
        or mcp_type_500 in payload
    )
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
    assert 'mcp_tool_requests_total{' in payload
    assert 'tool="unknown"' in payload


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


def test_probe_requests_do_not_pollute_tool_metrics(auth_client: TestClient) -> None:
    auth_client.get("/healthz")
    auth_client.get("/readyz")
    payload = auth_client.get("/metrics").text
    for line in payload.splitlines():
        if line.startswith("mcp_tool_requests_total{"):
            assert 'traffic_type="probe"' not in line


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
    assert re.search(
        r'mcp_tool_request_duration_seconds_count\{[^}]*method="CallToolRequest"[^}]*outcome="error"[^}]*\}',
        payload,
    )
    assert 'mcp_tool_errors_total{' in payload
