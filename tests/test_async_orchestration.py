from __future__ import annotations

import json

import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import (
    _execute_external_status_check,
    _normalize_polled_external_status_job,
    _resolve_execution_mode,
)
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.tools.registry import get_tool_specs


def test_resolve_execution_mode_auto_sync_in_dev() -> None:
    settings = Settings(_env_file=None, environment="development", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "sync"


def test_resolve_execution_mode_auto_async_in_production() -> None:
    settings = Settings(_env_file=None, environment="production", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "async"


def test_external_status_check_schema_contains_response_mode_and_check_id_polling_hint() -> None:
    spec = next(s for s in get_tool_specs() if s.name == "external_status_check")
    properties = spec.input_schema["properties"]

    assert properties["response_mode"]["default"] == "compact"
    assert properties["response_mode"]["enum"] == ["compact", "full"]
    assert "polls this job" in properties["check_id"]["description"]


@pytest.mark.asyncio
async def test_platform_api_jobs_client_submit_includes_internal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self._timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict, headers: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured["key"] = headers.get("X-Internal-Api-Key", "")
            captured["job_type"] = json["job_type"]
            return FakeResponse({"job_id": "job_123", "status": "queued", "created_at": "2026-01-01T00:00:00Z"})

    monkeypatch.setattr("incidentflow_mcp.platform_api.ai_jobs_client.httpx.AsyncClient", FakeAsyncClient)

    settings = Settings(
        _env_file=None,
        environment="test",
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="secret-key",
    )
    client = PlatformAPIJobsClient(settings)
    payload = {"job_type": "incident.summary.generate"}
    response = await client.submit_job(payload)

    assert response["job_id"] == "job_123"
    assert captured["url"] == "http://platform.test/api/v1/ai/jobs"
    assert captured["key"] == "secret-key"
    assert captured["job_type"] == "incident.summary.generate"


def test_normalize_polled_external_status_job_running_returns_async() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_1",
        job={"status": "running"},
        poll_after_seconds=2,
        response_mode="compact",
    )
    payload = json.loads(output)

    assert payload["mode"] == "async"
    assert payload["job_id"] == "job_1"
    assert payload["status"] == "running"
    assert payload["poll_after_seconds"] == 2


def test_normalize_polled_external_status_job_terminal_returns_compact_payload() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_2",
        job={
            "status": "succeeded",
            "result": {
                "status": "success",
                "action": "fetched_external_status",
                "providers_succeeded": 1,
                "external_status": [
                    {
                        "provider": "github",
                        "indicator": "minor",
                        "description": "Degraded",
                        "incidents": [
                            {
                                "id": "inc_1",
                                "name": "Incident 1",
                                "status": "investigating",
                                "impact": "minor",
                                "created_at": "2026-03-17T00:00:00Z",
                                "updated_at": "2026-03-17T00:10:00Z",
                                "shortlink": "https://status/1",
                                "incident_updates": [{"body": "very large payload"}],
                            }
                        ],
                        "degraded_components": [{"name": "Actions", "status": "degraded_performance"}],
                    }
                ],
            },
            "artifact_refs": ["artifact_1"],
            "usage": {"tokens": 1},
            "updated_at": "2026-03-17T18:00:00Z",
        },
        poll_after_seconds=2,
        response_mode="compact",
    )
    payload = json.loads(output)

    assert payload["mode"] == "completed"
    assert payload["status"] == "succeeded"
    compact_incident = payload["result"]["external_status"][0]["incidents"][0]
    assert compact_incident["id"] == "inc_1"
    assert "incident_updates" not in compact_incident
    assert payload["artifact_refs"] == ["artifact_1"]
    assert payload["response_mode"] == "compact"


def test_normalize_polled_external_status_job_terminal_returns_full_payload() -> None:
    raw_result = {
        "status": "success",
        "external_status": [
            {
                "provider": "github",
                "incidents": [
                    {
                        "id": "inc_1",
                        "name": "Incident 1",
                        "incident_updates": [{"body": "full payload"}],
                    }
                ],
            }
        ],
    }

    output = _normalize_polled_external_status_job(
        job_id="job_2",
        job={"status": "succeeded", "result": raw_result},
        poll_after_seconds=2,
        response_mode="full",
    )
    payload = json.loads(output)

    assert payload["mode"] == "completed"
    assert payload["response_mode"] == "full"
    assert payload["result"] == raw_result


@pytest.mark.asyncio
async def test_external_status_check_starts_new_job_when_check_id_missing() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.get_calls = 0

        async def submit_job(self, payload: dict) -> dict:
            self.submit_calls += 1
            assert payload["job_type"] == "alert.group.summary.generate"
            assert payload["payload"]["providers"] == ["aws"]
            return {"job_id": "new_job", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:
            self.get_calls += 1
            return {"job_id": job_id, "status": "running"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["aws"],
        workspace_id="ws_1",
        check_id=None,
        wait_for_result=False,
        response_mode="compact",
    )
    payload = json.loads(output)

    assert fake_client.submit_calls == 1
    assert fake_client.get_calls == 0
    assert payload["mode"] == "async"
    assert payload["job_id"] == "new_job"
    assert payload["status"] == "queued"


@pytest.mark.asyncio
async def test_external_status_check_polls_existing_job_when_check_id_present() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.get_calls = 0

        async def submit_job(self, payload: dict) -> dict:
            self.submit_calls += 1
            return {"job_id": "unused", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:
            self.get_calls += 1
            assert job_id == "existing_job"
            return {
                "job_id": job_id,
                "status": "failed",
                "error": {"category": "retryable", "reason": "provider timeout"},
            }

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["aws", "github"],
        workspace_id="ws_1",
        check_id="existing_job",
        response_mode="compact",
    )
    payload = json.loads(output)

    assert fake_client.submit_calls == 0
    assert fake_client.get_calls == 1
    assert payload["mode"] == "completed"
    assert payload["job_id"] == "existing_job"
    assert payload["status"] == "failed"
    assert payload["error"]["reason"] == "provider timeout"
    assert payload["response_mode"] == "compact"
