from typing import Any

import pytest

from incidentflow_mcp.tools.investigations import investigation_get, investigation_list


class FakeInvestigationsClient:
    def __init__(self, *, detail: dict[str, Any], listing: dict[str, Any] | None = None) -> None:
        self.detail = detail
        self.listing = listing or {"items": []}

    async def list(self, *, needs_attention: bool | None, limit: int) -> dict[str, Any]:
        return self.listing

    async def get(self, investigation_id: str) -> dict[str, Any]:
        return self.detail

    async def continue_(self, investigation_id: str, *, reason: str | None) -> dict[str, Any]:
        raise AssertionError("not used")

    async def recheck(self, investigation_id: str) -> dict[str, Any]:
        raise AssertionError("not used")


def _detail_payload() -> dict[str, Any]:
    return {
        "id": "be02f333-7333-4072-bf56-e44b4572715c",
        "status": "monitoring",
        "title": "Controlled Kubernetes ImagePullBackOff",
        "severity": "warning",
        "needs_attention": True,
        "scope": {"namespace": "incidentflow-e2e", "workload_name": "replicas-mismatch"},
        "ui_path": "/investigations/be02f333-7333-4072-bf56-e44b4572715c",
        "initial_assessment": {"impact_level": "confirmed"},
        "latest_assessment": {"root_cause": "ErrImagePull"},
        "current_health": {"status": "investigating"},
        "evidence_summary": {"items": [], "returned": 0, "limit": 20},
        "recent_timeline": {"items": [], "returned": 0, "limit": 20},
        # Bounded compatibility aliases consumed by the web UI, not MCP.
        "evidence": [{"evidence_id": "E01"}],
        "timeline": [{"type": "investigation_assessed"}],
    }


@pytest.mark.asyncio
async def test_get_projects_public_api_payload_onto_strict_mcp_contract() -> None:
    output = await investigation_get(
        FakeInvestigationsClient(detail=_detail_payload()),
        investigation_id="be02f333-7333-4072-bf56-e44b4572715c",
    )

    payload = output.model_dump(mode="json")
    assert payload["latest_assessment"]["root_cause"] == "ErrImagePull"
    assert "evidence" not in payload
    assert "timeline" not in payload


@pytest.mark.asyncio
async def test_list_projects_future_public_api_fields_from_items() -> None:
    item = {
        key: value
        for key, value in _detail_payload().items()
        if key
        in {
            "id",
            "status",
            "title",
            "severity",
            "needs_attention",
            "scope",
            "ui_path",
        }
    }
    item.update(
        {
            "impact_level": "confirmed",
            "confidence_level": "high",
            "future_platform_field": "must-not-leak",
        }
    )
    client = FakeInvestigationsClient(
        detail=_detail_payload(),
        listing={"items": [item], "total": 1},
    )

    output = await investigation_list(client, needs_attention=None, limit=20)

    payload = output.model_dump(mode="json")
    assert len(payload["items"]) == 1
    assert "future_platform_field" not in payload["items"][0]
    assert "total" not in payload
