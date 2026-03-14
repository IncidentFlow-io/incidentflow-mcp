"""Observability metrics coverage for HTTP/MCP middleware."""

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

    healthz_probe = 'http_requests_total{method="GET",route="/healthz",status_code="200",traffic="probe"}'
    readyz_probe = 'http_requests_total{method="GET",route="/readyz",status_code="200",traffic="probe"}'
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
