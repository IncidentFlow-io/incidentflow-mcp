"""Contract tests for the canonical versioned MCP response envelope.

Covers the five representative tools selected for the first rollout:
``mcp_version``, ``external_status_check``, ``k8s_agent_status``,
``argocd_connection_health`` and ``public_knowledge_search``.

Verifies:
- every response is the same 8-key envelope;
- success and error payloads validate against the generated JSON Schema;
- output schemas are published per-tool (not a generic blanket schema);
- tool errors set ``isError=True`` and carry a canonical error code;
- the error code in the body matches the one recorded for telemetry.
"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest
from mcp.types import CallToolResult

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.contracts import (
    ENVELOPE_SCHEMA_ID,
    ERROR_SCHEMA_ID,
    ErrorCode,
    build_output_schema,
    envelope_schema,
    error_envelope,
    error_schema,
    export_tool_schemas,
    response_schema_id,
    success_envelope,
)
from incidentflow_mcp.tools.registry import get_tool_specs

ENVELOPE_KEYS = {
    "api_version",
    "schema_version",
    "schema_id",
    "status",
    "request_id",
    "data",
    "error",
    "meta",
}

SELECTED_TOOLS = (
    "mcp_version",
    "external_status_check",
    "k8s_agent_status",
    "argocd_connection_health",
    "public_knowledge_search",
)


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        incidentflow_pat="test-secret-token",
        environment="development",
        redis_url="redis://test-only",
        mcp_default_workspace_id="ws_dev",
    )


# --- envelope structure -----------------------------------------------------
def test_success_envelope_has_exactly_the_eight_keys() -> None:
    env = success_envelope({"x": 1}, tool_name="mcp_version")
    assert set(env) == ENVELOPE_KEYS
    assert env["status"] == "success"
    assert env["api_version"] == "v1"
    assert env["schema_version"] == "1.0"
    assert env["schema_id"] == "incidentflow.mcp-version.response"
    assert env["error"] is None
    assert env["request_id"].startswith("req_")
    assert set(env["meta"]) == {"generated_at", "truncated", "warnings"}


def test_error_envelope_matches_reference_shape() -> None:
    env = error_envelope(
        tool_name="external_status_check",
        code=ErrorCode.INVALID_ARGUMENT,
        message="Invalid job_id format",
        details={"field": "job_id", "expected": "uuid"},
    )
    assert set(env) == ENVELOPE_KEYS
    assert env["status"] == "error"
    assert env["data"] is None
    assert env["schema_id"] == "incidentflow.external-status-check.response"
    assert env["error"] == {
        "code": "INVALID_ARGUMENT",
        "message": "Invalid job_id format",
        "retryable": False,
        "details": {"field": "job_id", "expected": "uuid"},
    }


@pytest.mark.parametrize(
    ("code", "expected_retryable"),
    [
        (ErrorCode.INVALID_ARGUMENT, False),
        (ErrorCode.NOT_FOUND, False),
        (ErrorCode.RATE_LIMITED, True),
        (ErrorCode.TIMEOUT, True),
        (ErrorCode.UPSTREAM_ERROR, True),
        (ErrorCode.INTEGRATION_UNAVAILABLE, True),
    ],
)
def test_error_default_retryability(code: ErrorCode, expected_retryable: bool) -> None:
    env = error_envelope(tool_name="mcp_version", code=code, message="x")
    assert env["error"]["retryable"] is expected_retryable


def test_all_ten_canonical_error_codes_exist() -> None:
    assert {c.value for c in ErrorCode} == {
        "INVALID_ARGUMENT",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "CONFLICT",
        "RATE_LIMITED",
        "INTEGRATION_UNAVAILABLE",
        "UPSTREAM_ERROR",
        "TIMEOUT",
        "INTERNAL_ERROR",
    }


# --- JSON Schema validation -------------------------------------------------
@pytest.mark.parametrize("tool_name", SELECTED_TOOLS)
def test_success_and_error_validate_against_generated_schema(tool_name: str) -> None:
    schema = build_output_schema(tool_name)
    # A minimal but representative success payload per tool.
    data_by_tool = {
        "mcp_version": {
            "service": "incidentflow-mcp",
            "service_version": "1.0.54",
            "current_api_version": "v1",
            "contract_version": "1.0",
            "supported_api_versions": ["v1"],
            "supported_schema_versions": ["1.0"],
            "deprecated_api_versions": [],
            "environment": "dev",
            "tools": {"registered": 46, "operational": 42, "meta": 4},
            "image": {"signed": False, "signature_verified": False},
        },
        "external_status_check": {
            "mode": "async",
            "job_id": "job_123",
            "job_status": "queued",
            "poll_after_seconds": 2,
        },
        "k8s_agent_status": {
            "status": "connected",
            "healthy": True,
            "checked_at": "2026-08-07T09:29:30Z",
        },
        "argocd_connection_health": {"source": {}, "healthy": True, "status": "ok"},
        "public_knowledge_search": {
            "query": "IncidentFlow API",
            "scope": "public",
            "results": [],
            "total": 0,
        },
    }
    success = success_envelope(data_by_tool[tool_name], tool_name=tool_name)
    jsonschema.validate(success, schema)

    error = error_envelope(
        tool_name=tool_name,
        code=ErrorCode.UPSTREAM_ERROR,
        message="boom",
    )
    jsonschema.validate(error, schema)


def test_common_error_schema_is_draft_2020_12() -> None:
    schema = error_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert ERROR_SCHEMA_ID in schema["$id"]
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        {"code": "TIMEOUT", "message": "x", "retryable": True},
        schema,
    )


def test_envelope_schema_is_generic_when_no_tool() -> None:
    schema = envelope_schema()
    assert ENVELOPE_SCHEMA_ID in schema["$id"]
    jsonschema.Draft202012Validator.check_schema(schema)


def test_per_tool_schema_ids_are_distinct() -> None:
    ids = {name: response_schema_id(name) for name in SELECTED_TOOLS}
    assert len(set(ids.values())) == len(SELECTED_TOOLS)
    assert ids["external_status_check"] == "incidentflow.external-status-check.response"


# --- published output schemas ----------------------------------------------
def test_registered_output_schemas_are_precise_per_tool() -> None:
    mcp = create_mcp_server()
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    for name in SELECTED_TOOLS:
        schema = tools[name].fn_metadata.output_schema
        assert schema is not None
        # Not a generic blanket object: schema_id is pinned to this tool.
        assert schema["properties"]["schema_id"]["const"] == response_schema_id(name)
        assert schema["additionalProperties"] is False


# --- end-to-end through the wrapper -----------------------------------------
@pytest.mark.asyncio
async def test_mcp_version_end_to_end_is_enveloped_and_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("incidentflow_mcp.config._settings", _test_settings())
    mcp = create_mcp_server()
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}

    result = await mcp._tool_manager.call_tool("mcp_version", {})
    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "success"
    jsonschema.validate(result, tools["mcp_version"].fn_metadata.output_schema)

    data = result["data"]
    assert data["service"] == "incidentflow-mcp"
    assert data["current_api_version"] == "v1"
    assert data["supported_api_versions"] == ["v1"]
    assert data["supported_schema_versions"] == ["1.0"]
    assert data["deprecated_api_versions"] == []
    assert data["service_version"]  # non-empty, single source


@pytest.mark.asyncio
async def test_tool_error_sets_is_error_and_canonical_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("incidentflow_mcp.config._settings", _test_settings())
    mcp = create_mcp_server()

    # argocd_connection_health with no workspace integration -> guard rejection.
    result = await mcp._tool_manager.call_tool("argocd_connection_health", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is True

    envelope = result.structuredContent
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["status"] == "error"
    code = envelope["error"]["code"]
    assert code in {c.value for c in ErrorCode}

    # Same code is echoed in the serialized text content (single source of truth).
    import json

    text_envelope = json.loads(result.content[0].text)
    assert text_envelope["error"]["code"] == code


# --- offline export ---------------------------------------------------------
def test_export_writes_common_and_per_tool_schemas(tmp_path: Path) -> None:
    specs = get_tool_specs()
    written = export_tool_schemas(specs, tmp_path)

    # common error + common envelope + one file per tool
    assert len(written) == len(specs) + 2
    assert (tmp_path / "incidentflow.common.error.schema.json").exists()
    assert (tmp_path / "incidentflow.common.envelope.schema.json").exists()
    assert (tmp_path / "incidentflow.mcp-version.response.schema.json").exists()
    assert (tmp_path / "incidentflow.external-status-check.response.schema.json").exists()
