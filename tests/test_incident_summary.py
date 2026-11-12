"""
Unit tests for the incident_summary tool.
"""

import pytest
from pydantic import ValidationError

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.registration import async_jobs
from incidentflow_mcp.tools.incident_summary import (
    incident_summary,
    incident_summary_from_platform,
)
from incidentflow_mcp.tools.schemas import IncidentSummaryInput, Severity


class TestIncidentSummaryKnownId:
    def test_returns_correct_incident_id(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert result.incident_id == "INC-001"

    def test_critical_incident_severity(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert result.severity == Severity.CRITICAL

    def test_title_is_non_empty(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert result.title

    def test_summary_is_non_empty(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert result.summary

    def test_timeline_returned_by_default(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert len(result.timeline) > 0

    def test_timeline_excluded_when_disabled(self) -> None:
        result = incident_summary(
            IncidentSummaryInput(incident_id="INC-001", include_timeline=False)
        )
        assert result.timeline == []

    def test_affected_services_returned_by_default(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert len(result.affected_services) > 0

    def test_affected_services_excluded_when_disabled(self) -> None:
        result = incident_summary(
            IncidentSummaryInput(incident_id="INC-001", include_affected_services=False)
        )
        assert result.affected_services == []

    def test_recommendations_present(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-001"))
        assert len(result.recommendations) > 0

    def test_second_known_incident(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-002"))
        assert result.incident_id == "INC-002"
        assert result.severity == Severity.HIGH


class TestIncidentSummaryUnknownId:
    def test_unknown_id_returns_gracefully(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-DOES-NOT-EXIST"))
        assert result.incident_id == "INC-DOES-NOT-EXIST"
        assert result.severity == Severity.INFO
        assert "not_found" in result.status

    def test_unknown_id_has_empty_timeline(self) -> None:
        result = incident_summary(IncidentSummaryInput(incident_id="INC-UNKNOWN"))
        assert result.timeline == []


def test_platform_summary_returns_resolved_incident_from_workspace_read_model() -> None:
    result = incident_summary_from_platform(
        IncidentSummaryInput(incident_id="ca4436cc-eaaa-4e79-a6aa-6ea02a045ca2"),
        {
            "id": "ca4436cc-eaaa-4e79-a6aa-6ea02a045ca2",
            "workspace_id": "5e647670-a2c6-4994-b7fb-5e3a331bea82",
            "title": "Pod is crash looping",
            "status": "resolved",
            "severity": "sev1",
            "summary_current": "Pod recovered",
            "primary_service_ref": "details-v1",
            "contributing_signals": [],
        },
        {"events": []},
    )

    assert result.status == "resolved"
    assert result.incident_id == "ca4436cc-eaaa-4e79-a6aa-6ea02a045ca2"
    assert result.affected_services == ["details-v1"]


@pytest.mark.asyncio
async def test_registered_incident_summary_reads_resolved_incident_from_platform(
    monkeypatch,
) -> None:
    incident_id = "ca4436cc-eaaa-4e79-a6aa-6ea02a045ca2"
    workspace_id = "5e647670-a2c6-4994-b7fb-5e3a331bea82"
    platform_incident = {
        "id": incident_id,
        "workspace_id": workspace_id,
        "title": "Pod is crash looping",
        "status": "resolved",
        "severity": "sev1",
        "summary_current": "Pod recovered",
        "primary_service_ref": "details-v1",
        "contributing_signals": [],
    }

    class FakePlatformClient:
        def __init__(self, _settings, *, bearer_token: str) -> None:
            assert bearer_token == "workspace-token"

        async def get(self, requested_id: str) -> dict:
            assert requested_id == incident_id
            return platform_incident

        async def get_timeline(self, requested_id: str) -> dict:
            assert requested_id == incident_id
            return {"events": []}

    class FakeMemory:
        async def consult_memory(self, **kwargs):
            assert kwargs["workspace_id"] == workspace_id
            return None

    class FakeMCP:
        tools = {}

        def tool(self, **metadata):
            def register(function):
                self.tools[metadata["name"]] = function
                return function

            return register

    monkeypatch.setattr(async_jobs, "PlatformIncidentsClient", FakePlatformClient)
    mcp = FakeMCP()
    context = type(
        "Context",
        (),
        {
            "settings": Settings(_env_file=None, platform_api_base_url="http://platform"),
            "mcp": mcp,
            "metadata": staticmethod(lambda name: {"name": name}),
        },
    )()
    async_jobs.register_async_tools(
        context,
        memory=FakeMemory(),
        current_token_workspace_id=lambda: workspace_id,
        current_bearer_token=lambda: "workspace-token",
    )

    result = await mcp.tools["incident_summary"](incident_id=incident_id, execution_mode="sync")

    assert result["incident_id"] == incident_id
    assert result["status"] == "resolved"


class TestIncidentSummaryInputValidation:
    def test_empty_incident_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            IncidentSummaryInput(incident_id="")

    def test_too_long_incident_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            IncidentSummaryInput(incident_id="X" * 129)
