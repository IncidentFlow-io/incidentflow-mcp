"""Async job orchestration helpers for MCP tools."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.schemas import Alert

logger = logging.getLogger(__name__)

VALID_EXECUTION_MODES = {"auto", "sync", "async"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "canceled"}
VALID_RESPONSE_MODES = {"compact", "full"}


def resolve_execution_mode(settings: Settings, requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    if mode == "auto":
        return "async" if settings.async_tools_enabled() else "sync"
    return mode


def resolve_external_status_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    # This tool is runner-backed by design; keep behavior deterministic in local dev.
    return "async"


def resolve_correlation_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode: {requested_mode}")
    if mode == "async":
        raise ValueError(
            "correlate_alerts async mode is disabled until a dedicated "
            "alert.correlation.generate runner exists; use execution_mode=sync or auto"
        )
    return "sync"


def normalize_correlation_alerts(
    alerts: list[Alert] | list[dict[str, Any]] | None,
    alerts_json: str | None,
) -> list[Alert]:
    payload: Any = alerts
    if payload is None and alerts_json:
        try:
            payload = json.loads(alerts_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"alerts_json must be valid JSON: {exc.msg}") from exc

    if payload is None:
        raise ValueError("Either alerts or legacy alerts_json must be provided")
    if not isinstance(payload, list):
        raise ValueError("alerts must be a list of alert objects")

    return [item if isinstance(item, Alert) else Alert.model_validate(item) for item in payload]


def resolve_response_mode(requested_mode: str) -> str:
    mode = requested_mode.lower().strip()
    if mode not in VALID_RESPONSE_MODES:
        raise ValueError(f"Unsupported response_mode: {requested_mode}")
    return mode


def resolve_job_workspace_id(
    workspace_id: str | None,
    *,
    token_workspace_id: str | None = None,
    default_workspace_id: str | None = None,
) -> str:
    explicit_workspace_id = workspace_id.strip() if workspace_id is not None else ""
    token_workspace_id_normalized = (
        token_workspace_id.strip() if token_workspace_id is not None else ""
    )

    if explicit_workspace_id:
        if token_workspace_id_normalized and explicit_workspace_id != token_workspace_id_normalized:
            raise ValueError(
                "workspace_scope_mismatch: explicit workspace_id does not match "
                "token workspace scope"
            )
        return explicit_workspace_id

    if token_workspace_id_normalized:
        return token_workspace_id_normalized

    if default_workspace_id is not None and default_workspace_id.strip():
        return default_workspace_id.strip()

    raise ValueError(
        "workspace_id is required for async job orchestration. "
        "Pass workspace_id or configure MCP_DEFAULT_WORKSPACE_ID."
    )


def normalize_providers(providers: list[str] | None) -> list[str]:
    if not providers:
        return ["aws", "github"]

    allowed = {"aws", "github"}
    normalized = [item.strip().lower() for item in providers if item.strip()]
    if not normalized:
        return ["aws", "github"]

    invalid = [item for item in normalized if item not in allowed]
    if invalid:
        raise ValueError(f"Unsupported provider(s): {', '.join(invalid)}")

    seen: set[str] = set()
    ordered: list[str] = []
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def build_async_result(
    *,
    job_id: str,
    status: str,
    poll_after_seconds: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "async",
        "job_id": job_id,
        "status": status,
        "poll_after_seconds": poll_after_seconds,
    }
    if extra:
        payload.update(extra)
    return payload


def compact_incident(incident: Any) -> dict[str, Any]:
    if not isinstance(incident, dict):
        return {"name": str(incident)}

    latest_update = None
    updates = incident.get("incident_updates")
    if isinstance(updates, list) and updates:
        update_dicts = [item for item in updates if isinstance(item, dict)]
        latest_update = max(
            update_dicts,
            key=lambda item: (
                compact_incident_update_timestamp(item) or datetime.min.replace(tzinfo=UTC)
            ),
            default=None,
        )

    return {
        "id": incident.get("id"),
        "name": incident.get("name") or incident.get("title"),
        "status": incident.get("status"),
        "impact": incident.get("impact"),
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "shortlink": incident.get("shortlink") or incident.get("link"),
        "updates_count": incident.get("updates_count")
        or (len(updates) if isinstance(updates, list) else None),
        "latest_update_status": latest_update.get("status") if latest_update else None,
        "latest_update_at": (
            latest_update.get("updated_at")
            or latest_update.get("created_at")
            or latest_update.get("display_at")
            if latest_update
            else None
        ),
    }


def compact_incident_update_timestamp(update: dict[str, Any]) -> datetime | None:
    timestamp = update.get("updated_at") or update.get("created_at") or update.get("display_at")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def compact_degraded_component(component: Any) -> dict[str, Any]:
    if not isinstance(component, dict):
        return {"name": str(component)}

    return {
        "id": component.get("id"),
        "name": component.get("name"),
        "status": component.get("status"),
        "description": component.get("description"),
        "updated_at": component.get("updated_at"),
    }


def incident_is_active(incident: dict[str, Any]) -> bool:
    status = str(incident.get("status") or "").lower()
    return status not in {"resolved", "completed", "postmortem", "closed"}


def compact_provider_status(provider_status: dict[str, Any]) -> dict[str, Any]:
    incidents_raw = provider_status.get("incidents")
    incidents_list = incidents_raw if isinstance(incidents_raw, list) else []
    compact_incidents = [compact_incident(item) for item in incidents_list[:20]]
    active_incidents = [incident for incident in compact_incidents if incident_is_active(incident)]
    all_historical_incidents = [
        incident for incident in compact_incidents if not incident_is_active(incident)
    ]
    max_historical_incidents = 5
    historical_incidents = all_historical_incidents[:max_historical_incidents]
    historical_total = max(0, len(incidents_list) - len(active_incidents))

    degraded_raw = provider_status.get("degraded_components")
    degraded_list = degraded_raw if isinstance(degraded_raw, list) else []
    compact_degraded = [compact_degraded_component(item) for item in degraded_list[:20]]

    compact: dict[str, Any] = {
        "provider": provider_status.get("provider"),
        "status": "ok",
        "indicator": provider_status.get("indicator"),
        "description": provider_status.get("description"),
        "active_incidents": active_incidents,
        "historical_incidents": historical_incidents,
        "historical_incidents_total": historical_total,
        "degraded_components": compact_degraded,
        "fetched_at": provider_status.get("fetched_at"),
        "truncated": len(incidents_list) > len(active_incidents) + len(historical_incidents),
    }

    if "regional_status" in provider_status:
        compact["regional_status"] = provider_status.get("regional_status") or {}
    if "regional_status_errors" in provider_status:
        compact["regional_status_errors"] = provider_status.get("regional_status_errors") or {}

    return compact


def compact_external_status_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result

    external_status = result.get("external_status")
    if not isinstance(external_status, list):
        return result

    compact_statuses: list[dict[str, Any]] = []
    for provider_status in external_status:
        if not isinstance(provider_status, dict):
            continue
        compact_statuses.append(compact_provider_status(provider_status))

    errors = result.get("errors")
    errors_list = errors if isinstance(errors, list) else []
    checked_at = None
    for provider_status in compact_statuses:
        fetched_at = provider_status.get("fetched_at")
        if isinstance(fetched_at, str) and (checked_at is None or fetched_at > checked_at):
            checked_at = fetched_at

    status = "ok"
    if errors_list and compact_statuses:
        status = "partial"
    elif errors_list and not compact_statuses:
        status = "error"
    elif str(result.get("status") or "").lower() not in {"success", "ok"}:
        status = str(result.get("status") or "unknown")

    provider_names = {
        str(provider_status.get("provider") or "").lower()
        for provider_status in compact_statuses
        if provider_status.get("provider")
    }
    for error in errors_list:
        if not isinstance(error, dict):
            continue
        provider = str(error.get("provider") or "").lower()
        if not provider or provider in provider_names:
            continue
        compact_statuses.append(
            {
                "provider": provider,
                "status": "error",
                "error": error.get("message") or error.get("error") or "provider failed",
                "error_type": error.get("error_type"),
                "source_url": error.get("source_url"),
                "status_code": error.get("status_code"),
                "active_incidents": [],
                "historical_incidents": [],
                "historical_incidents_total": 0,
                "degraded_components": [],
            }
        )
        provider_names.add(provider)

    compact_result = {
        "status": status,
        "checked_at": checked_at,
        "providers": compact_statuses,
    }
    if errors_list:
        compact_result["errors"] = errors_list
    if "persistence" in result:
        compact_result["persistence"] = result.get("persistence")
    if "provenance" in result:
        compact_result["provenance"] = result.get("provenance")
    return compact_result


def normalize_polled_external_status_job(
    *,
    job_id: str,
    job: dict[str, Any],
    poll_after_seconds: int,
    response_mode: str,
) -> dict[str, Any]:
    status = str(job.get("status", "unknown"))
    if not polled_job_matches(job, expected_job_type="alert.group.summary.generate"):
        return polled_job_mismatch_result(
            job_id=job_id,
            status=status,
            expected_tool="external_status_check",
            expected_job_type="alert.group.summary.generate",
        )

    if status in {"admitted", "queued", "dispatched", "running"}:
        return build_async_result(
            job_id=job_id,
            status=status,
            poll_after_seconds=poll_after_seconds,
        )

    if status in TERMINAL_JOB_STATUSES:
        normalized_result = (
            compact_external_status_result(job.get("result"))
            if response_mode == "compact"
            else job.get("result")
        )
        if response_mode == "compact" and status == "succeeded":
            return (
                normalized_result
                if isinstance(normalized_result, dict)
                else {"status": status, "result": normalized_result}
            )
        payload: dict[str, Any] = {
            "mode": "completed",
            "job_id": job_id,
            "status": status,
            "result": normalized_result,
            "error": job.get("error"),
            "artifact_refs": safe_artifact_refs(job.get("artifact_refs", [])),
            "usage": job.get("usage"),
            "updated_at": job.get("updated_at"),
            "response_mode": response_mode,
        }
        return payload

    return build_async_result(
        job_id=job_id,
        status=status,
        poll_after_seconds=poll_after_seconds,
    )


def normalize_polled_incident_summary_job(
    *,
    job_id: str,
    job: dict[str, Any],
    poll_after_seconds: int,
) -> dict[str, Any]:
    status = str(job.get("status", "unknown"))
    if not polled_job_matches(job, expected_job_type="incident.summary.generate"):
        return polled_job_mismatch_result(
            job_id=job_id,
            status=status,
            expected_tool="incident_summary",
            expected_job_type="incident.summary.generate",
        )

    if status in TERMINAL_JOB_STATUSES:
        payload: dict[str, Any] = {
            "mode": "completed",
            "job_id": job_id,
            "status": status,
            "result": job.get("result"),
            "error": job.get("error"),
            "artifact_refs": safe_artifact_refs(job.get("artifact_refs", [])),
            "usage": job.get("usage"),
            "updated_at": job.get("updated_at"),
        }
        return payload

    return build_async_result(
        job_id=job_id,
        status=status,
        poll_after_seconds=poll_after_seconds,
    )


def safe_artifact_refs(artifact_refs: Any) -> list[str]:
    if not isinstance(artifact_refs, list):
        return []
    return [
        artifact_ref
        for artifact_ref in artifact_refs
        if isinstance(artifact_ref, str) and not artifact_ref.startswith("mock_")
    ]


def polled_job_matches(job: dict[str, Any], *, expected_job_type: str) -> bool:
    observed_type = job.get("job_type") or job.get("type") or job.get("operation")
    if isinstance(observed_type, str):
        return observed_type == expected_job_type

    result = job.get("result")
    if not isinstance(result, dict):
        return True

    if expected_job_type == "incident.summary.generate":
        return "external_status" not in result and result.get("action") != "fetched_external_status"
    if expected_job_type == "alert.group.summary.generate":
        return "external_status" in result or result.get("action") == "fetched_external_status"
    return True


def polled_job_mismatch_result(
    *,
    job_id: str,
    status: str,
    expected_tool: str,
    expected_job_type: str,
) -> dict[str, Any]:
    return {
        "mode": "completed",
        "job_id": job_id,
        "status": "failed",
        "error": {
            "code": "JOB_OPERATION_MISMATCH",
            "message": (
                "check_id belongs to a different async operation; start a new "
                f"{expected_tool} job or poll the matching tool."
            ),
            "expected_job_type": expected_job_type,
            "observed_status": status,
        },
    }


async def poll_until_done(
    client: Any,
    job_id: str,
    initial_delay: int,
    max_wait_seconds: int = 45,
) -> dict[str, Any]:
    await asyncio.sleep(initial_delay)
    waited = initial_delay
    while waited < max_wait_seconds:
        job = await client.get_job(job_id)
        if str(job.get("status", "")) in TERMINAL_JOB_STATUSES:
            return job
        interval = 3
        if waited + interval > max_wait_seconds:
            interval = max_wait_seconds - waited
        if interval <= 0:
            break
        await asyncio.sleep(interval)
        waited += interval
    return await client.get_job(job_id)


async def execute_external_status_check(
    *,
    settings: Settings,
    client: Any,
    providers: list[str] | None,
    workspace_id: str | None,
    check_id: str | None,
    wait_for_result: bool = True,
    days_back: int = 30,
    response_mode: str = "compact",
    token_workspace_id: str | None = None,
    current_token_workspace_id: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    resolved_token_workspace_id = token_workspace_id
    if resolved_token_workspace_id is None and current_token_workspace_id is not None:
        resolved_token_workspace_id = current_token_workspace_id()
    resolved_workspace_id = resolve_job_workspace_id(
        workspace_id,
        token_workspace_id=resolved_token_workspace_id,
        default_workspace_id=settings.mcp_default_workspace_id,
    )
    selected_response_mode = resolve_response_mode(response_mode)
    selected_providers = normalize_providers(providers)

    if check_id:
        job = await client.get_job(check_id)
        if wait_for_result and str(job.get("status", "")) not in TERMINAL_JOB_STATUSES:
            job = await poll_until_done(
                client=client,
                job_id=check_id,
                initial_delay=settings.platform_api_ai_poll_after_seconds,
                max_wait_seconds=45,
            )
        return normalize_polled_external_status_job(
            job_id=check_id,
            job=job,
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            response_mode=selected_response_mode,
        )

    submitted = await client.submit_job(
        {
            "job_type": "alert.group.summary.generate",
            "runner_mode": "summary",
            "task_profile": "summary.small",
            "workspace_id": resolved_workspace_id,
            "incident_id": "external-status",
            "payload": {
                "providers": selected_providers,
                "external_status_only": True,
                "days_back": days_back,
                "persist_to_oms": settings.mcp_oms_persist_enabled,
            },
            "artifact_refs": [],
            "evidence_refs": [],
        }
    )

    job_id = submitted["job_id"]
    logger.info(
        "mcp_async_job_submitted tool=external_status_check job_id=%s workspace_id=%s",
        job_id,
        resolved_workspace_id,
    )

    if not wait_for_result:
        return build_async_result(
            job_id=job_id,
            status=submitted.get("status", "queued"),
            poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
            extra={"providers": selected_providers},
        )

    job = await poll_until_done(
        client=client,
        job_id=job_id,
        initial_delay=settings.platform_api_ai_poll_after_seconds,
        max_wait_seconds=45,
    )
    return normalize_polled_external_status_job(
        job_id=job_id,
        job=job,
        poll_after_seconds=settings.platform_api_ai_poll_after_seconds,
        response_mode=selected_response_mode,
    )
