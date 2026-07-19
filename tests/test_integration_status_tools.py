import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient
from incidentflow_mcp.platform_api.integration_status_client import PlatformIntegrationStatusClient


def _set_context() -> None:
    set_current_auth_context(
        {
            "authenticated": True,
            "auth_method": "oauth",
            "bearer_token": "token",
            "client_id": "oauth-client",
            "workspace_id": "ws_123",
            "workspace_name": "Demo Workspace",
            "workspace_slug": "demo",
            "workspace_role": "owner",
            "user_id": "user_123",
            "email": "demo@example.com",
            "plan": None,
        }
    )


def _payload(result: object) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


@pytest.fixture(autouse=True)
def _clear_auth_context() -> None:
    clear_current_auth_context()
    yield
    clear_current_auth_context()


@pytest.mark.asyncio
async def test_platform_integration_status_client_workspace_status_uses_internal_key() -> None:
    settings = Settings(
        _env_file=None,
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="internal",
        redis_url="redis://test-only",
    )
    response = Mock()
    response.content = b'{"ok":true}'
    response.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response) as get:
        payload = await PlatformIntegrationStatusClient(settings).get_workspace_status(
            workspace_id="ws_123"
        )

    assert payload == {"ok": True}
    response.raise_for_status.assert_called_once_with()
    get.assert_awaited_once()
    _, kwargs = get.await_args
    assert kwargs["params"] == {"workspace_id": "ws_123"}
    assert kwargs["headers"]["X-Internal-Api-Key"] == "internal"


@pytest.mark.asyncio
async def test_incidentflow_auth_status_returns_safe_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(_env_file=None, environment="development", redis_url="redis://test-only"),
    )
    _set_context()

    result = await create_mcp_server()._tool_manager.call_tool("incidentflow_auth_status", {})
    payload = _payload(result)

    assert payload == {
        "authenticated": True,
        "authMethod": "oauth",
        "client": {"name": "OAuth MCP client", "type": "mcp"},
        "user": {"email": "demo@example.com"},
        "workspace": {
            "id": "ws_123",
            "slug": "demo",
            "name": "Demo Workspace",
            "role": "owner",
        },
        "permissions": [
            "workspace.read",
            "integrations.read",
            "integrations.manage",
        ],
        "connectedIntegrations": [],
        "availableToolGroups": ["platform"],
        "environment": "dev",
    }
    assert "token" not in json.dumps(payload).lower()


@pytest.mark.asyncio
async def test_incidentflow_auth_status_redacts_token_like_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(_env_file=None, environment="development", redis_url="redis://test-only"),
    )
    _set_context()
    context = {
        "authenticated": True,
        "auth_method": "oauth",
        "bearer_token": "token",
        "client_id": "if_oac_example_token_like_identifier",
        "workspace_id": "ws_123",
        "workspace_name": "Demo Workspace",
        "workspace_slug": "demo",
        "workspace_role": "owner",
        "user_id": "user_123",
        "email": "demo@example.com",
        "plan": None,
    }
    set_current_auth_context(context)

    result = await create_mcp_server()._tool_manager.call_tool("incidentflow_auth_status", {})
    payload = _payload(result)

    assert payload["client"] == {"name": "OAuth MCP client", "type": "mcp"}
    assert "if_oac" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_incidentflow_auth_status_includes_connected_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            environment="development",
            platform_api_base_url="http://platform.test",
            platform_api_internal_api_key="internal",
            redis_url="redis://test-only",
        ),
    )
    _set_context()
    workspace_status = {
        "kubernetes": {"clusters": [{"name": "kind-local", "connected": True}]},
        "grafana": {"connected": True, "datasources": [{"uid": "prom"}]},
        "slack": {"connected": False},
        "argocd": {
            "connected": True,
            "display_name": "incidentflow",
            "application_count": 37,
        },
    }

    with patch.object(
        PlatformIntegrationStatusClient,
        "get_workspace_status",
        new=AsyncMock(return_value=workspace_status),
    ):
        result = await create_mcp_server()._tool_manager.call_tool(
            "incidentflow_auth_status",
            {},
        )

    payload = _payload(result)

    assert payload["connectedIntegrations"] == ["kubernetes", "grafana", "argocd"]
    assert payload["availableToolGroups"] == [
        "platform",
        "kubernetes",
        "grafana",
        "argocd",
    ]


@pytest.mark.asyncio
async def test_integrations_status_shows_shared_dev_kubernetes_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            environment="development",
            shared_dev_kubernetes_enabled=True,
            shared_dev_kubernetes_agent_id="cluster_dev",
            shared_dev_kubernetes_cluster_name="incidentflow-dev",
            redis_url="redis://test-only",
        ),
    )
    _set_context()

    result = await create_mcp_server()._tool_manager.call_tool(
        "incidentflow_integrations_status",
        {},
    )
    payload = _payload(result)

    assert payload["kubernetes"]["status"] == "connected"
    assert payload["kubernetes"]["source"] == "shared_dev"
    assert payload["kubernetes"]["workspaceIntegration"] == "not_connected"
    assert payload["kubernetes"]["workspaceActions"] == [
        {
            "type": "open_url",
            "label": "Connect Kubernetes",
            "url": "https://app-dev.incidentflow.io/integrations",
        },
        {
            "type": "open_url",
            "label": "Read setup guide",
            "url": "https://incidentflow.io/docs/integrations/kubernetes",
        },
    ]
    assert payload["kubernetes"]["effectiveConnection"] == {
        "type": "shared_dev_agent",
        "cluster": "incidentflow-dev",
        "environment": "dev",
    }
    assert payload["grafana"]["status"] == "not_connected"
    assert payload["slack"]["status"] == "not_connected"
    assert payload["slack"]["actions"] == [
        {
            "type": "open_url",
            "label": "Connect Slack",
            "url": "https://app-dev.incidentflow.io/integrations",
        },
        {
            "type": "open_url",
            "label": "Read setup guide",
            "url": "https://incidentflow.io/docs/integrations/slack",
        },
    ]
    assert payload["argocd"]["status"] == "not_connected"


@pytest.mark.asyncio
async def test_integrations_status_uses_workspace_platform_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            environment="development",
            platform_api_base_url="http://platform.test",
            redis_url="redis://test-only",
        ),
    )
    _set_context()

    async def list_clusters(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
    ) -> list[dict[str, object]]:
        assert bearer_token == "token"
        return [
            {
                "cluster_id": "cluster_123",
                "name": "incidentflow",
                "connected": True,
            }
        ]

    async def get_status(
        self: PlatformIntegrationStatusClient,
        integration: str,
        *,
        bearer_token: str,
    ) -> dict[str, object]:
        assert bearer_token == "token"
        return {
            "slack": {
                "connected": False,
                "status": "not_connected",
                "workspace_name": None,
            },
            "grafana": {
                "connected": True,
                "status": "connected",
                "datasources": [{"uid": "prom", "name": "Prometheus"}],
            },
            "argocd": {
                "id": "argocd_123",
                "connected": True,
                "status": "connected",
                "display_name": "Argo CD",
                "application_count": 37,
            },
        }[integration]

    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "list_clusters", list_clusters)
    monkeypatch.setattr(PlatformIntegrationStatusClient, "get_status", get_status)

    result = await create_mcp_server()._tool_manager.call_tool(
        "incidentflow_integrations_status",
        {},
    )
    payload = _payload(result)

    assert payload["slack"]["status"] == "not_connected"
    assert payload["slack"]["actions"][0]["label"] == "Connect Slack"
    assert payload["slack"]["actions"][1]["url"] == (
        "https://incidentflow.io/docs/integrations/slack"
    )
    assert payload["kubernetes"]["status"] == "connected"
    assert payload["kubernetes"]["displayName"] == "incidentflow"
    assert payload["grafana"]["status"] == "connected"
    assert payload["grafana"]["resourceCount"] == 1
    assert payload["argocd"]["status"] == "connected"
    assert payload["argocd"]["resourceCount"] == 37


@pytest.mark.asyncio
async def test_integrations_status_prefers_internal_workspace_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            environment="development",
            platform_api_base_url="http://platform.test",
            platform_api_internal_api_key="internal",
            redis_url="redis://test-only",
        ),
    )
    _set_context()

    async def get_workspace_status(
        self: PlatformIntegrationStatusClient,
        *,
        workspace_id: str,
    ) -> dict[str, object]:
        assert workspace_id == "ws_123"
        return {
            "kubernetes": {
                "clusters": [
                    {
                        "cluster_id": "cluster_123",
                        "name": "incidentflow",
                        "connected": True,
                    }
                ]
            },
            "grafana": {
                "connected": True,
                "status": "connected",
                "datasources": [{"uid": "prom", "name": "Prometheus"}],
            },
            "slack": {
                "connected": False,
                "status": "not_connected",
                "workspace_name": None,
            },
            "argocd": {
                "id": "argocd_123",
                "connected": True,
                "status": "connected",
                "display_name": "Argo CD",
                "application_count": 37,
            },
        }

    async def list_clusters(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
    ) -> list[dict[str, object]]:
        raise AssertionError("bearer-token cluster lookup should not be used")

    monkeypatch.setattr(
        PlatformIntegrationStatusClient,
        "get_workspace_status",
        get_workspace_status,
    )
    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "list_clusters", list_clusters)

    result = await create_mcp_server()._tool_manager.call_tool(
        "incidentflow_integrations_status",
        {},
    )
    payload = _payload(result)

    assert payload["slack"]["status"] == "not_connected"
    assert payload["slack"]["actions"][0]["url"] == "https://app-dev.incidentflow.io/integrations"
    assert payload["slack"]["actions"][1]["url"] == (
        "https://incidentflow.io/docs/integrations/slack"
    )
    assert payload["kubernetes"]["status"] == "connected"
    assert payload["grafana"]["status"] == "connected"
    assert payload["argocd"]["status"] == "connected"
    assert payload["argocd"]["resourceCount"] == 37


@pytest.mark.asyncio
async def test_grafana_tool_returns_standard_not_connected_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(_env_file=None, environment="development", redis_url="redis://test-only"),
    )
    _set_context()

    result = await create_mcp_server()._tool_manager.call_tool("grafana_list_dashboards", {})
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["code"] == "INTEGRATION_NOT_CONNECTED"
    assert payload["integration"] == "grafana"
    assert payload["status"] == "not_connected"
    assert payload["actions"][0]["url"] == "https://app-dev.incidentflow.io/integrations"


@pytest.mark.asyncio
async def test_argocd_tool_returns_standard_not_connected_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(_env_file=None, environment="development", redis_url="redis://test-only"),
    )
    _set_context()

    result = await create_mcp_server()._tool_manager.call_tool("argocd_connection_health", {})
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["code"] == "INTEGRATION_NOT_CONNECTED"
    assert payload["integration"] == "argocd"
    assert payload["status"] == "not_connected"
    assert payload["message"] == "Argo CD is not connected for the current workspace."
