"""Public-boundary contract checks for E2E-001 / IF-QA-020."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from incidentflow_mcp.app import create_app
from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.registry import get_submission_tool_specs
from incidentflow_mcp.tools.schemas import IncidentSummaryOutput

MEMORY_TOOLS = {
    "memory_search_similar_incidents",
    "memory_get_service_context",
    "memory_upsert_incident_summary",
    "memory_find_runbook",
}


def _call_tool(client: TestClient, *, mode: str) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": f"incident-summary-{mode}",
            "method": "tools/call",
            "params": {
                "name": "incident_summary",
                "arguments": {
                    "incident_id": "INC-001",
                    "execution_mode": mode,
                },
            },
        },
    )

    assert response.status_code == 200
    data_line = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    return json.loads(data_line)


def test_every_enabled_mode_returns_the_same_documented_summary_shape(
    unauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_job_is_submitted(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        pytest.fail("enabled incident_summary modes must not submit a platform job")

    monkeypatch.setattr(
        "incidentflow_mcp.platform_api.ai_jobs_client.PlatformAPIJobsClient.submit_job",
        fail_if_job_is_submitted,
    )

    with unauth_client as client:
        responses = [_call_tool(client, mode=mode) for mode in ("sync", "auto")]

    summaries: list[IncidentSummaryOutput] = []
    for response in responses:
        assert response["result"].get("isError") is not True
        content = response["result"]["content"]
        assert len(content) == 1
        summaries.append(IncidentSummaryOutput.model_validate_json(content[0]["text"]))

    assert summaries[0] == summaries[1]
    assert summaries[0].incident_id == "INC-001"


def test_unavailable_async_mode_fails_explicitly_before_job_submission(
    unauth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_job_is_submitted(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        pytest.fail("unavailable incident_summary mode must fail before platform submission")

    monkeypatch.setattr(
        "incidentflow_mcp.platform_api.ai_jobs_client.PlatformAPIJobsClient.submit_job",
        fail_if_job_is_submitted,
    )

    with unauth_client as client:
        response = _call_tool(client, mode="async")

    assert response["result"]["isError"] is True
    error_text = response["result"]["content"][0]["text"]
    assert "no runner currently implements" in error_text
    assert "external_status_check" in error_text
    assert "job_id" not in error_text


def test_production_auto_fails_explicitly_when_it_would_select_unavailable_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        allow_unprotected_in_production=True,
        incidentflow_pat=None,
        platform_api_base_url=None,
        redis_url="redis://test-only",
        log_level="warning",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

    async def fail_if_job_is_submitted(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        pytest.fail("production auto must fail before platform submission")

    monkeypatch.setattr(
        "incidentflow_mcp.platform_api.ai_jobs_client.PlatformAPIJobsClient.submit_job",
        fail_if_job_is_submitted,
    )

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = _call_tool(client, mode="auto")

    assert response["result"]["isError"] is True
    error_text = response["result"]["content"][0]["text"]
    assert "outside explicit demo/test mode" in error_text
    assert "synthetic incidents" in error_text
    assert "job_id" not in error_text


@pytest.mark.parametrize("mode", ["sync", "auto", "async"])
def test_production_modes_cannot_return_synthetic_demo_data(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        allow_unprotected_in_production=True,
        incidentflow_pat=None,
        platform_api_base_url=None,
        redis_url="redis://test-only",
        log_level="warning",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = _call_tool(client, mode=mode)

    assert response["result"]["isError"] is True
    error_text = response["result"]["content"][0]["text"]
    assert "outside explicit demo/test mode" in error_text
    assert "synthetic incidents" in error_text
    assert "job_id" not in error_text


def test_unavailable_release_contracts_are_excluded_from_public_submission() -> None:
    submission_names = {spec.name for spec in get_submission_tool_specs()}

    assert "incident_summary" not in submission_names
    assert MEMORY_TOOLS.isdisjoint(submission_names)


@pytest.mark.asyncio
async def test_production_discovery_omits_memory_and_stale_calls_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        allow_unprotected_in_production=True,
        incidentflow_pat=None,
        platform_api_base_url=None,
        redis_url="redis://test-only",
        log_level="warning",
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)
    server = create_app().state.mcp_server

    discovered = {tool.name for tool in await server.list_tools()}
    assert MEMORY_TOOLS.isdisjoint(discovered)

    for tool_name in MEMORY_TOOLS:
        with pytest.raises(ToolError, match=f"Unknown tool: {tool_name}"):
            await server.call_tool(tool_name, {})
