"""Argo CD read tools for MCP."""

from __future__ import annotations

from typing import Any, Protocol

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
    client: ArgoCDReadClient, *, name: str, integration_id: str | None = None
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.get_application(name=name, integration_id=integration_id)
    )


async def argocd_get_application_resources(
    client: ArgoCDReadClient, *, name: str, integration_id: str | None = None
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.get_application_resources(name=name, integration_id=integration_id)
    )


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
    client: ArgoCDReadClient, *, name: str, integration_id: str | None = None
) -> ArgoCDOutput:
    return ArgoCDOutput.model_validate(
        await client.analyze_application(name=name, integration_id=integration_id)
    )
