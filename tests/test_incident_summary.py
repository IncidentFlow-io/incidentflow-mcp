"""
Unit tests for the incident_summary tool.
"""

import pytest

from incidentflow_mcp.tools.incident_summary import incident_summary
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


class TestIncidentSummaryInputValidation:
    def test_empty_incident_id_raises(self) -> None:
        with pytest.raises(Exception):
            IncidentSummaryInput(incident_id="")

    def test_too_long_incident_id_raises(self) -> None:
        with pytest.raises(Exception):
            IncidentSummaryInput(incident_id="X" * 129)
