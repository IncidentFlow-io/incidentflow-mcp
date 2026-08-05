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


async def argocd_get_last_operation(
    client: ArgoCDReadClient, *, name: str, integration_id: str | None = None
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.get_last_operation(name=name, integration_id=integration_id)
    )


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
