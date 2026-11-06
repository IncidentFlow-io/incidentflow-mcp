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


def _reference_aliases(payload: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    evidence_summary = payload.get("evidence_summary")
    evidence_items = evidence_summary.get("items", []) if isinstance(evidence_summary, dict) else []
    evidence_aliases = {
        str(item["id"]): str(item["evidence_id"])
        for item in evidence_items
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("evidence_id"), str)
    }

    recent_timeline = payload.get("recent_timeline")
    timeline_items = recent_timeline.get("items", []) if isinstance(recent_timeline, dict) else []
    timeline_aliases = {
        str(item["id"]): str(item["reference_id"])
        for item in timeline_items
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("reference_id"), str)
    }
    return evidence_aliases, timeline_aliases


def _project_citation_references(
    value: Any,
    *,
    evidence_aliases: dict[str, str],
    timeline_aliases: dict[str, str],
    key: str | None = None,
) -> Any:
    """Replace stored evidence/timeline citation UUIDs and drop unmapped refs."""
    evidence_reference_keys = {
        "evidence_id",
        "evidence_ids",
        "support_evidence_ids",
        "contradiction_evidence_ids",
    }

    def mapped_reference(reference: Any, aliases: dict[str, str], prefix: str) -> str | None:
        if not isinstance(reference, str):
            return None
        if len(reference) == 3 and reference.startswith(prefix) and reference[1:].isdigit():
            return reference
        return aliases.get(reference)

    if key in evidence_reference_keys:
        if isinstance(value, list):
            return [
                alias
                for item in value
                if (alias := mapped_reference(item, evidence_aliases, "E")) is not None
            ]
        if isinstance(value, str):
            return mapped_reference(value, evidence_aliases, "E")
        return value
    if key == "timeline_event_refs":
        if isinstance(value, list):
            return [
                alias
                for item in value
                if (alias := mapped_reference(item, timeline_aliases, "T")) is not None
            ]
        if isinstance(value, str):
            return mapped_reference(value, timeline_aliases, "T")
        return value
    if isinstance(value, dict):
        return {
            str(child_key): _project_citation_references(
                child,
                evidence_aliases=evidence_aliases,
                timeline_aliases=timeline_aliases,
                key=str(child_key),
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _project_citation_references(
                child,
                evidence_aliases=evidence_aliases,
                timeline_aliases=timeline_aliases,
            )
            for child in value
        ]
    return value


def _project_investigation_citations(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize all assessment/pass citation fields to response-scoped aliases."""
    evidence_aliases, timeline_aliases = _reference_aliases(payload)
    for key in ("assessment", "initial_assessment", "latest_assessment"):
        assessment = payload.get(key)
        if isinstance(assessment, dict):
            payload[key] = _project_citation_references(
                assessment,
                evidence_aliases=evidence_aliases,
                timeline_aliases=timeline_aliases,
            )
    passes = payload.get("passes")
    if isinstance(passes, list):
        payload["passes"] = [
            _project_citation_references(
                pass_item,
                evidence_aliases=evidence_aliases,
                timeline_aliases=timeline_aliases,
            )
            if isinstance(pass_item, dict)
            else pass_item
            for pass_item in passes
        ]
    return payload


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
    payload = _project_investigation_citations(payload)
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
