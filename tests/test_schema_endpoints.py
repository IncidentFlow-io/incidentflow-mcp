"""Tests for the schema-publication HTTP endpoints (/version, /schemas)."""

from __future__ import annotations

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentflow_mcp.config import Settings
from incidentflow_mcp.http.routers.ops import create_ops_router
from incidentflow_mcp.tools.contracts import ENVELOPE_SCHEMA_ID, ERROR_SCHEMA_ID


@pytest.fixture
def client() -> TestClient:
    settings = Settings(_env_file=None, environment="development", redis_url="redis://test-only")
    app = FastAPI()
    app.include_router(create_ops_router(settings))
    return TestClient(app)


def test_version_endpoint_publishes_contract_block(client: TestClient) -> None:
    body = client.get("/version").json()
    assert body["service"] == "incidentflow-mcp"
    assert body["api_version"] == "v1"
    assert body["contract_version"] == "1.0"
    assert body["supported_api_versions"] == ["v1"]
    assert body["supported_schema_versions"] == ["1.0"]
    assert body["service_version"]  # non-empty


def test_schemas_catalog_lists_common_and_per_tool(client: TestClient) -> None:
    body = client.get("/schemas").json()
    assert body["api_version"] == "v1"
    ids = {entry["schema_id"] for entry in body["schemas"]}
    assert ENVELOPE_SCHEMA_ID in ids
    assert ERROR_SCHEMA_ID in ids
    assert "incidentflow.mcp-version.response" in ids
    assert "incidentflow.external-status-check.response" in ids


def test_schema_by_id_returns_valid_draft_2020_12(client: TestClient) -> None:
    schema = client.get("/schemas/incidentflow.mcp-version.response").json()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_id"]["const"] == "incidentflow.mcp-version.response"


def test_unknown_schema_id_returns_404(client: TestClient) -> None:
    assert client.get("/schemas/incidentflow.does-not-exist").status_code == 404


def _mcp_version_environment(payload: object) -> str:
    data = payload if isinstance(payload, dict) else {}
    if "schema_id" in data and "data" in data:
        return data["data"]["environment"]
    return data["environment"]  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # No build metadata → falls back to ENVIRONMENT (normalized).
        ({"environment": "development"}, "dev"),
        # Explicit build environment wins and is normalized to a canonical lane.
        ({"environment": "production", "mcp_build_environment": "dev"}, "dev"),
        # Release tag with no explicit build environment → production lane.
        ({"environment": "development", "mcp_build_tag": "v1.0.0"}, "production"),
    ],
)
async def test_http_and_mcp_version_use_same_environment(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict, expected: str
) -> None:
    """/version and mcp_version must report the identical, canonical lane."""
    from incidentflow_mcp.mcp.server import create_mcp_server

    settings = Settings(_env_file=None, redis_url="redis://test-only", **kwargs)
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

    app = FastAPI()
    app.include_router(create_ops_router(settings))
    http_environment = TestClient(app).get("/version").json()["environment"]

    result = await create_mcp_server()._tool_manager.call_tool("mcp_version", {})
    mcp_environment = _mcp_version_environment(
        result.structuredContent if hasattr(result, "structuredContent") else result
    )

    assert http_environment == mcp_environment == expected
