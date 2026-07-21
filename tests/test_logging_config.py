import json
import logging

from incidentflow_mcp.logging_config import configure_logging


def test_json_logging_outputs_parseable_structured_line(
    capsys,
) -> None:
    configure_logging(level="info", library_level="warning", log_format="json")
    logger = logging.getLogger("incidentflow_mcp.tests.logging")

    logger.info(
        "http_request token=secret",
        extra={
            "http_method": "POST",
            "http_route": "/mcp",
            "http_status_code": 200,
            "http_duration_ms": 12.34,
            "log_message": "request completed token=secret",
            "mcp_request_type": "unknown",
            "workspace_id": "",
            "cluster_id": None,
            "color_message": "ignored",
        },
    )

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)

    assert payload["event"] == "http_request token=***"
    assert payload["level"] == "INFO"
    assert payload["service"] == "incidentflow-mcp"
    assert payload["service_version"] == "0.1.0"
    assert payload["environment"] == "dev"
    assert payload["logger"] == "incidentflow_mcp.tests.logging"
    assert payload["http_method"] == "POST"
    assert payload["http_route"] == "/mcp"
    assert payload["http_status_code"] == 200
    assert payload["http_duration_ms"] == 12.34
    assert payload["message"] == "request completed token=***"
    assert "log_message" not in payload
    assert "mcp_request_type" not in payload
    assert "workspace_id" not in payload
    assert "cluster_id" not in payload
    assert "color_message" not in payload
    assert "timestamp" in payload
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_text_logging_omits_empty_trace_fields(capsys) -> None:
    configure_logging(level="info", library_level="warning", log_format="text")
    logger = logging.getLogger("incidentflow_mcp.tests.logging")

    logger.info(
        "http_request",
        extra={
            "http_method": "POST",
            "http_route": "/mcp",
            "http_status_code": 200,
            "color_message": "ignored",
        },
    )

    line = capsys.readouterr().out.strip()

    assert "http_request" in line
    assert "service_version=0.1.0" in line
    assert "environment=dev" in line
    assert "http_method=POST" in line
    assert "http_route=/mcp" in line
    assert "http_status_code=200" in line
    assert "color_message" not in line
    assert "trace_id=" not in line
    assert "span_id=" not in line
