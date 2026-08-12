"""Typed MCP payload models for the durable investigation control surface."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class InvestigationsClient(Protocol):
    async def list(self, *, needs_attention: bool | None, limit: int) -> dict[str, Any]: ...
    async def get(self, investigation_id: str) -> dict[str, Any]: ...
    async def continue_(self, investigation_id: str, *, reason: str | None) -> dict[str, Any]: ...
    async def recheck(self, investigation_id: str) -> dict[str, Any]: ...


class InvestigationListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: str
    title: str
    severity: str
    needs_attention: bool
    summary: str | None = None
    recommended_action: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    impact_level: str
    confidence_level: str
    ui_path: str
    stop_reason: str | None = None
    evidence_used: int = 0
    evidence_budget: int = 0
    queued_at: Any | None = None
    started_at: Any | None = None
    assessed_at: Any | None = None
    resolved_at: Any | None = None


class InvestigationListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[InvestigationListItem]


class InvestigationGetOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: str
    title: str
    severity: str
    needs_attention: bool
    scope: dict[str, Any]
    ui_path: str
    initial_assessment: dict[str, Any] = Field(default_factory=dict)
    latest_assessment: dict[str, Any] = Field(default_factory=dict)
    current_health: dict[str, Any] = Field(default_factory=dict)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    recent_timeline: dict[str, Any] = Field(default_factory=dict)
    impact_level: str = "unknown"
    confidence_level: str = "unknown"
    summary: str | None = None
    recommended_action: str | None = None
    stop_reason: str | None = None
    assessment: dict[str, Any] = Field(default_factory=dict)
    assessment_evidence: list[dict[str, Any]] = Field(default_factory=list)
    passes: list[dict[str, Any]] = Field(default_factory=list)
    evidence_budget: int = 0
    evidence_used: int = 0
    queued_at: Any | None = None
    started_at: Any | None = None
    assessed_at: Any | None = None
    resolved_at: Any | None = None


class InvestigationPassOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    investigation_id: str
    mode: str
    ui_path: str
    job_id: str | None = None
    existing_job_id: str | None = None
    reason: str | None = None
    current_status: str | None = None
    existing_evidence_count: int | None = None


def _contract_fields(model: type[BaseModel], payload: dict[str, Any]) -> dict[str, Any]:
    """Project a public API payload onto the strict MCP output contract.

    Platform API intentionally keeps bounded legacy aliases such as ``evidence``
    and ``timeline`` for the web UI. They are not part of the compact MCP
    contract and must not leak into generated tool output schemas.
    """
    return {name: payload[name] for name in model.model_fields if name in payload}


async def investigation_list(
    client: InvestigationsClient, *, needs_attention: bool | None, limit: int
) -> InvestigationListOutput:
    payload = await client.list(needs_attention=needs_attention, limit=limit)
    raw_items = payload.get("items", [])
    items = [
        InvestigationListItem.model_validate(_contract_fields(InvestigationListItem, item))
        for item in raw_items
        if isinstance(item, dict)
    ]
    return InvestigationListOutput(
        items=items,
    )


async def investigation_get(
    client: InvestigationsClient, *, investigation_id: str
) -> InvestigationGetOutput:
    payload = await client.get(investigation_id)
    return InvestigationGetOutput.model_validate(_contract_fields(InvestigationGetOutput, payload))


async def investigation_continue(
    client: InvestigationsClient, *, investigation_id: str, reason: str | None
) -> InvestigationPassOutput:
    payload = await client.continue_(investigation_id, reason=reason)
    return InvestigationPassOutput.model_validate(
        _contract_fields(InvestigationPassOutput, payload)
    )


async def investigation_recheck(
    client: InvestigationsClient, *, investigation_id: str
) -> InvestigationPassOutput:
    payload = await client.recheck(investigation_id)
    return InvestigationPassOutput.model_validate(
        _contract_fields(InvestigationPassOutput, payload)
    )
