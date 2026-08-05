"""Unit tests for the Argo CD MCP read tools (fake client, no network)."""

from __future__ import annotations

from typing import Any

from incidentflow_mcp.tools.argocd import (
    argocd_analyze_application,
    argocd_connection_health,
    argocd_find_recent_deployments,
    argocd_get_application,
    argocd_get_application_resources,
    argocd_get_last_operation,
    argocd_get_sync_history,
    argocd_list_applications,
)


def _base_payload(**extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "source": {
            "type": "argocd",
            "integration_id": "int-1",
            "integration_name": "incidentflow",
            "server_url": "https://argocd.example.com",
            "fetched_at": "2026-07-14T12:00:00+00:00",
        },
        "truncated": False,
        "warnings": [],
    }


class FakeClient:
    def __init__(self, **payloads: Any) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def health(self, *, integration_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("health", {"integration_id": integration_id}))
        return self._payloads.get("health", _base_payload(ok=True))

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
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "list_applications",
                {
                    "integration_id": integration_id,
                    "search": search,
                    "project": project,
                    "namespace": namespace,
                    "destination_cluster": destination_cluster,
                    "health_status": health_status,
                    "sync_status": sync_status,
                    "limit": limit,
                },
            )
        )
        return self._payloads.get("list_applications", _base_payload(applications=[]))

    async def get_application(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("get_application", {"name": name, "integration_id": integration_id}))
        return self._payloads.get("get_application", _base_payload(application={"name": name}))

    async def get_application_resources(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_application_resources", {"name": name, "integration_id": integration_id})
        )
        return self._payloads.get("get_application_resources", _base_payload(resources=[]))

    async def get_sync_history(
        self, *, name: str, integration_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_sync_history", {"name": name, "integration_id": integration_id, "limit": limit})
        )
        return self._payloads.get("get_sync_history", _base_payload(history=[]))

    async def get_last_operation(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("get_last_operation", {"name": name, "integration_id": integration_id}))
        return self._payloads.get("get_last_operation", _base_payload(operation=None))

    async def find_recent_deployments(
        self,
        *,
        integration_id: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "find_recent_deployments",
                {
                    "integration_id": integration_id,
                    "project": project,
                    "namespace": namespace,
                    "limit": limit,
                },
            )
        )
        return self._payloads.get("find_recent_deployments", _base_payload(deployments=[]))

    async def analyze_application(
        self, *, name: str, integration_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("analyze_application", {"name": name, "integration_id": integration_id}))
        return self._payloads.get("analyze_application", _base_payload(summary={}))


async def test_connection_health_keeps_source_metadata() -> None:
    out = await argocd_connection_health(FakeClient(), integration_id="int-1")
    assert out.source["type"] == "argocd"
    assert out.source["integration_id"] == "int-1"
    assert "token" not in out.model_dump()


async def test_list_applications_forwards_filters() -> None:
    client = FakeClient(
        list_applications=_base_payload(
            applications=[{"name": "checkout", "sync_status": "Synced"}], returned=1
        )
    )

    out = await argocd_list_applications(
        client,
        search="checkout",
        project="default",
        namespace="prod",
        sync_status="Synced",
        limit=10,
    )

    assert out.model_dump()["applications"][0]["name"] == "checkout"
    assert client.calls == [
        (
            "list_applications",
            {
                "integration_id": None,
                "search": "checkout",
                "project": "default",
                "namespace": "prod",
                "destination_cluster": None,
                "health_status": None,
                "sync_status": "Synced",
                "limit": 10,
            },
        )
    ]


async def test_application_tools_forward_name_and_limits() -> None:
    client = FakeClient()

    await argocd_get_application(client, name="checkout")
    await argocd_get_application_resources(client, name="checkout")
    await argocd_get_sync_history(client, name="checkout", limit=7)
    await argocd_get_last_operation(client, name="checkout")
    await argocd_find_recent_deployments(client, project="default", limit=3)
    await argocd_analyze_application(client, name="checkout")

    assert client.calls == [
        ("get_application", {"name": "checkout", "integration_id": None}),
        ("get_application_resources", {"name": "checkout", "integration_id": None}),
        ("get_sync_history", {"name": "checkout", "integration_id": None, "limit": 7}),
        ("get_last_operation", {"name": "checkout", "integration_id": None}),
        (
            "find_recent_deployments",
            {"integration_id": None, "project": "default", "namespace": None, "limit": 3},
        ),
        ("analyze_application", {"name": "checkout", "integration_id": None}),
    ]


async def test_application_compact_mode_trims_nested_history_and_operation_results() -> None:
    client = FakeClient(
        get_application=_base_payload(
            application={
                "name": "checkout",
                "history": [{"id": idx} for idx in range(6)],
                "operation": {
                    "phase": "Succeeded",
                    "resource_results": [{"name": f"resource-{idx}"} for idx in range(12)],
                },
            }
        )
    )

    out = await argocd_get_application(client, name="checkout", history_limit=2)
    payload = out.model_dump()
    app = payload["application"]

    assert payload["truncated"] is True
    assert app["history"] == [{"id": 0}, {"id": 1}]
    assert app["history_returned"] == 2
    assert app["history_truncated"] is True
    assert len(app["operation"]["resource_results"]) == 10
    assert app["operation"]["resource_results_truncated"] is True
    assert "Application history trimmed to 2 entries." in payload["warnings"]


async def test_application_full_mode_keeps_nested_payload() -> None:
    client = FakeClient(
        get_application=_base_payload(
            application={"name": "checkout", "history": [{"id": idx} for idx in range(3)]}
        )
    )

    out = await argocd_get_application(client, name="checkout", response_mode="full")

    payload = out.model_dump()
    assert payload["truncated"] is False
    assert len(payload["application"]["history"]) == 3
    assert "history_returned" not in payload["application"]


async def test_resources_compact_mode_trims_resource_tree() -> None:
    client = FakeClient(
        get_application_resources=_base_payload(
            resources=[{"kind": "Deployment", "name": f"resource-{idx}"} for idx in range(4)]
        )
    )

    out = await argocd_get_application_resources(client, name="checkout", limit=2)
    payload = out.model_dump()

    assert payload["truncated"] is True
    assert payload["returned"] == 2
    assert payload["total"] == 4
    assert [resource["name"] for resource in payload["resources"]] == ["resource-0", "resource-1"]
    assert "Resource tree trimmed to 2 resources." in payload["warnings"]
