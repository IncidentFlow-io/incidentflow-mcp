"""Argo CD read tools for MCP."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ArgoCDReadClient(Protocol):
    async def health(self, *, integration_id: str | None = None) -> dict[str, Any]: ...
    async def list_applications(
        self,
        *,
        integration_id: str | None = None,
        search: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        destination_cluster: str | None = None,
        health_status: str | None = None,
        sync_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...
    async def get_application(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]: ...
    async def get_application_resources(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]: ...
    async def get_sync_history(
        self, *, name: str, integration_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]: ...
    async def get_last_operation(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]: ...
    async def find_recent_deployments(
        self,
        *,
        integration_id: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...
    async def analyze_application(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]: ...


class ArgoCDOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: dict[str, Any]
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


ResponseMode = Literal["compact", "full"]


def _append_warning(payload: dict[str, Any], warning: str) -> None:
    warnings = payload.setdefault("warnings", [])
    if isinstance(warnings, list) and warning not in warnings:
        warnings.append(warning)


def _compact_history(history: Any, *, limit: int) -> tuple[list[Any], bool]:
    if not isinstance(history, list):
        return [], False
    return history[:limit], len(history) > limit


def _compact_operation(operation: Any, *, resource_limit: int = 10) -> Any:
    if not isinstance(operation, dict):
        return operation
    compact = dict(operation)
    resource_results = compact.get("resource_results")
    if isinstance(resource_results, list) and len(resource_results) > resource_limit:
        compact["resource_results"] = resource_results[:resource_limit]
        compact["resource_results_returned"] = resource_limit
        compact["resource_results_total"] = len(resource_results)
        compact["resource_results_truncated"] = True
    return compact


def _compact_application_payload(payload: dict[str, Any], *, history_limit: int) -> dict[str, Any]:
    compact = dict(payload)
    application = compact.get("application")
    if not isinstance(application, dict):
        return compact

    app = dict(application)
    history, history_truncated = _compact_history(app.get("history"), limit=history_limit)
    if "history" in app:
        app["history"] = history
        app["history_returned"] = len(history)
        app["history_truncated"] = history_truncated
        if history_truncated:
            compact["truncated"] = True
            _append_warning(compact, f"Application history trimmed to {history_limit} entries.")

    if "operation" in app:
        app["operation"] = _compact_operation(app["operation"])

    compact["application"] = app
    return compact


def _compact_resources_payload(payload: dict[str, Any], *, resource_limit: int) -> dict[str, Any]:
    compact = dict(payload)
    resources = compact.get("resources")
    if not isinstance(resources, list):
        return compact

    total = len(resources)
    compact["resources"] = resources[:resource_limit]
    compact["returned"] = min(total, resource_limit)
    compact["total"] = compact.get("total", total)
    if total > resource_limit:
        compact["truncated"] = True
        _append_warning(compact, f"Resource tree trimmed to {resource_limit} resources.")
    return compact


async def argocd_connection_health(
    client: ArgoCDReadClient, *, integration_id: str | None = None
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(await client.health(integration_id=integration_id))


async def argocd_list_applications(
    client: ArgoCDReadClient,
    *,
    integration_id: str | None = None,
    search: str | None = None,
    project: str | None = None,
    namespace: str | None = None,
    destination_cluster: str | None = None,
    health_status: str | None = None,
    sync_status: str | None = None,
    limit: int = 50,
) -> ArgoCDOutput:
    payload = await client.list_applications(
        integration_id=integration_id,
        search=search,
        project=project,
        namespace=namespace,
        destination_cluster=destination_cluster,
        health_status=health_status,
        sync_status=sync_status,
        limit=limit,
    )
    return ArgoCDOutput.model_validate(payload)


async def argocd_get_application(
    client: ArgoCDReadClient,
    *,
    name: str,
    integration_id: str | None = None,
    response_mode: ResponseMode = "compact",
    history_limit: int = 5,
) -> ArgoCDOutput:
    payload = await client.get_application(name=name, integration_id=integration_id)
    if response_mode == "compact":
        payload = _compact_application_payload(payload, history_limit=history_limit)
    return ArgoCDOutput.model_validate(payload)


async def argocd_get_application_resources(
    client: ArgoCDReadClient,
    *,
    name: str,
    integration_id: str | None = None,
    limit: int = 50,
    response_mode: ResponseMode = "compact",
) -> ArgoCDOutput:
    payload = await client.get_application_resources(name=name, integration_id=integration_id)
    if response_mode == "compact":
        payload = _compact_resources_payload(payload, resource_limit=limit)
    return ArgoCDOutput.model_validate(payload)


async def argocd_get_sync_history(
    client: ArgoCDReadClient,
    *,
    name: str,
    integration_id: str | None = None,
    limit: int = 20,
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.get_sync_history(name=name, integration_id=integration_id, limit=limit)
    )


# Argo CD per-resource sync statuses that need no attention.
_OK_RESOURCE_STATUSES = frozenset({"Synced", "Pruned"})
_RESOURCE_MESSAGE_LIMIT = 300


def _resource_problem(resource: dict[str, Any]) -> dict[str, Any]:
    message = resource.get("message")
    if isinstance(message, str) and len(message) > _RESOURCE_MESSAGE_LIMIT:
        message = message[:_RESOURCE_MESSAGE_LIMIT] + "…"
    return {
        "group": resource.get("group"),
        "kind": resource.get("kind"),
        "namespace": resource.get("namespace"),
        "name": resource.get("name"),
        "status": resource.get("status"),
        "message": message,
    }


def _compact_last_operation_payload(
    payload: dict[str, Any], *, resource_limit: int
) -> dict[str, Any]:
    """Replace the per-resource list with counts plus a bounded list of problem resources."""

    compact = dict(payload)
    operation = compact.get("operation")
    if not isinstance(operation, dict):
        return compact
    resources = operation.get("resource_results")
    resources = (
        [item for item in resources if isinstance(item, dict)]
        if isinstance(resources, list)
        else []
    )
    by_status: dict[str, int] = {}
    for item in resources:
        key = str(item.get("status") or "Unknown")
        by_status[key] = by_status.get(key, 0) + 1
    problems = [item for item in resources if item.get("status") not in _OK_RESOURCE_STATUSES]
    compact["operation"] = {
        "phase": operation.get("phase"),
        "message": operation.get("message"),
        "started_at": operation.get("started_at"),
        "finished_at": operation.get("finished_at"),
        "sync_revision": operation.get("sync_revision"),
        "resource_summary": {
            "total": len(resources),
            "by_status": by_status,
            "problem_count": len(problems),
        },
        "resource_errors": [_resource_problem(item) for item in problems[:resource_limit]],
        "resource_errors_truncated": len(problems) > resource_limit,
    }
    compact["response_mode"] = "compact"
    if len(problems) > resource_limit:
        compact["truncated"] = True
        _append_warning(
            compact,
            f"Problem resources trimmed to {resource_limit} of {len(problems)}; "
            "raise resource_limit or use response_mode=full.",
        )
    return compact


async def argocd_get_last_operation(
    client: ArgoCDReadClient,
    *,
    name: str,
    integration_id: str | None = None,
    response_mode: ResponseMode = "compact",
    resource_limit: int = 10,
) -> ArgoCDOutput:
    payload = await client.get_last_operation(name=name, integration_id=integration_id)
    if response_mode == "compact" and isinstance(payload, dict):
        payload = _compact_last_operation_payload(payload, resource_limit=resource_limit)
    return ArgoCDOutput.model_validate(payload)


async def argocd_find_recent_deployments(
    client: ArgoCDReadClient,
    *,
    integration_id: str | None = None,
    project: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.find_recent_deployments(
            integration_id=integration_id,
            project=project,
            namespace=namespace,
            limit=limit,
        )
    )


async def argocd_analyze_application(
    client: ArgoCDReadClient,
    *,
    name: str,
    integration_id: str | None = None,
    response_mode: ResponseMode = "compact",
    history_limit: int = 5,
) -> ArgoCDOutput:
    payload = await client.analyze_application(name=name, integration_id=integration_id)
    if response_mode == "compact":
        payload = _compact_application_payload(payload, history_limit=history_limit)
    return ArgoCDOutput.model_validate(payload)
