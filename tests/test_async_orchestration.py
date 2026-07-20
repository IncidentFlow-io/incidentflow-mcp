from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import (
    _analyze_workload_logs,
    _build_describe_response,
    _cluster_health_assessment,
    _compact_external_status_result,
    _compact_log_payload,
    _describe_pod_structured,
    _diagnose_pod,
    _execute_external_status_check,
    _filter_workload_pods,
    _k8s_cluster_overview_payload,
    _k8s_connection_health_payload,
    _k8s_rbac_check_payload,
    _normalize_correlation_alerts,
    _normalize_polled_external_status_job,
    _normalize_polled_incident_summary_job,
    _overview_payload,
    _resolve_correlation_mode,
    _resolve_execution_mode,
    _resolve_job_workspace_id,
    _resolve_k8s_cluster_id,
    _resolve_slack_tool_access,
    _restart_window_summary,
    _select_workload_pod,
    _structured_tool_exception,
    create_mcp_server,
)
from incidentflow_mcp.platform_api.agent_commands_client import PlatformAPIAgentCommandsClient
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.tools.registry import get_tool_specs


def _payload(result: object) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


class FakeAgentClusterClient:
    def __init__(self, clusters: list[dict]) -> None:
        self.clusters = clusters
        self.list_calls = 0

    async def list_clusters(self, *, bearer_token: str) -> list[dict]:
        assert bearer_token == "token"
        self.list_calls += 1
        return self.clusters


class FakeK8sHealthClient(FakeAgentClusterClient):
    def __init__(self, clusters: list[dict], responses: dict[tuple[str, str], dict]) -> None:
        super().__init__(clusters)
        self.responses = responses
        self.dispatch_calls: list[tuple[str, dict]] = []

    async def send_agent_command(
        self,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict,
        timeout_seconds: int | None = None,
    ) -> dict:
        assert bearer_token == "token"
        assert cluster_id == "cluster_prod"
        _ = timeout_seconds
        namespace = str(params.get("namespace") or "")
        self.dispatch_calls.append((action, params))
        return self.responses.get(
            (action, namespace),
            {
                "command_id": "cmd",
                "status": "failed",
                "error": {"code": "missing_fixture", "message": f"No fixture for {action}"},
            },
        )


class FailingK8sDispatchClient(FakeAgentClusterClient):
    async def send_agent_command(
        self,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict,
        timeout_seconds: int | None = None,
    ) -> dict:
        _ = bearer_token, cluster_id, action, params, timeout_seconds
        raise httpx.ConnectError("agent gateway unavailable")


def _set_k8s_tool_context() -> None:
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


def test_resolve_execution_mode_auto_sync_in_dev() -> None:
    settings = Settings(_env_file=None, environment="development", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "sync"


def test_resolve_execution_mode_auto_async_in_production() -> None:
    settings = Settings(_env_file=None, environment="production", mcp_async_tools_enabled=None)
    assert _resolve_execution_mode(settings, "auto") == "async"


def test_correlate_alerts_auto_stays_sync_and_async_is_rejected() -> None:
    assert _resolve_correlation_mode("auto") == "sync"
    assert _resolve_correlation_mode("sync") == "sync"
    with pytest.raises(ValueError, match=r"alert\.correlation\.generate"):
        _resolve_correlation_mode("async")


def _sample_alert_payload() -> dict:
    return {
        "alert_id": "slack-1779307031.278049",
        "name": "InstanceDown",
        "severity": "critical",
        "fired_at": "2026-05-20T19:57:11.278049+00:00",
        "status": "firing",
        "service": "kubernetes-pods-annotated",
        "labels": {
            "cluster": "minikube",
            "namespace": "cert-manager",
            "pod": "cert-manager-cainjector-5d5f946fd-8jp45",
        },
    }


def test_correlate_alerts_accepts_alerts_and_legacy_alerts_json() -> None:
    payload = _sample_alert_payload()

    direct = _normalize_correlation_alerts([payload], None)
    legacy = _normalize_correlation_alerts(None, json.dumps([payload]))

    assert direct[0].name == "InstanceDown"
    assert legacy[0].alert_id == "slack-1779307031.278049"


def test_correlate_alerts_rejects_invalid_payload_cleanly() -> None:
    with pytest.raises(ValueError, match="alerts must be a list"):
        _normalize_correlation_alerts({"name": "InstanceDown"}, None)  # type: ignore[arg-type]


def test_select_workload_pod_prefers_exact_then_prefix() -> None:
    pods = [{"name": "checkout-api-abc"}, {"name": "checkout-api"}]
    assert _select_workload_pod(pods, "checkout-api") == "checkout-api"
    assert (
        _select_workload_pod([{"name": "checkout-api-abc"}], "checkout-api") == "checkout-api-abc"
    )


def test_incident_summary_schema_supports_check_id_polling() -> None:
    spec = next(s for s in get_tool_specs() if s.name == "incident_summary")
    properties = spec.input_schema["properties"]
    assert "check_id" in properties
    assert "wait_for_result" in properties
    # incident_id must no longer be strictly required — polling uses check_id instead.
    assert spec.input_schema["required"] == []


def test_external_status_check_schema_contains_response_mode_and_check_id_polling_hint() -> None:
    spec = next(s for s in get_tool_specs() if s.name == "external_status_check")
    properties = spec.input_schema["properties"]

    assert properties["response_mode"]["default"] == "compact"
    assert properties["response_mode"]["enum"] == ["compact", "full"]
    assert "polls this job" in properties["check_id"]["description"]


def test_resolve_job_workspace_id_prefers_explicit_scope_or_default() -> None:
    with pytest.raises(ValueError):
        _resolve_job_workspace_id(None)
    assert (
        _resolve_job_workspace_id(
            None,
            default_workspace_id="35b02121-716b-4097-a851-84485d39b76f",
        )
        == "35b02121-716b-4097-a851-84485d39b76f"
    )
    assert (
        _resolve_job_workspace_id(
            "   ",
            default_workspace_id="35b02121-716b-4097-a851-84485d39b76f",
        )
        == "35b02121-716b-4097-a851-84485d39b76f"
    )
    assert _resolve_job_workspace_id("ws_1", default_workspace_id="ws_default") == "ws_1"
    assert (
        _resolve_job_workspace_id(
            None,
            token_workspace_id="35b02121-716b-4097-a851-84485d39b76f",
            default_workspace_id="ws_default",
        )
        == "35b02121-716b-4097-a851-84485d39b76f"
    )


def test_resolve_job_workspace_id_rejects_workspace_scope_mismatch() -> None:
    with pytest.raises(ValueError, match="workspace_scope_mismatch"):
        _resolve_job_workspace_id(
            "ws_explicit",
            token_workspace_id="ws_from_token",
            default_workspace_id="ws_default",
        )


def test_slack_tool_access_prefers_platform_mode_over_legacy_token() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        platform_api_base_url="https://platform.example",
        platform_api_internal_api_key="internal-token",
        slack_bot_token="xoxb-legacy",
    )

    token, client = _resolve_slack_tool_access(
        settings,
        workspace_id=None,
        token_workspace_id="ws_from_token",
    )

    assert token is None
    assert client is not None
    assert client._workspace_id == "ws_from_token"


def test_slack_tool_access_rejects_direct_token_in_production() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        slack_bot_token="xoxb-legacy",
    )

    with pytest.raises(ValueError, match="slack_platform_mode_required"):
        _resolve_slack_tool_access(
            settings,
            workspace_id=None,
            token_workspace_id="ws_from_token",
        )


def test_slack_tool_access_allows_local_legacy_token_with_workspace_scope() -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        slack_bot_token="xoxb-local",
    )

    token, client = _resolve_slack_tool_access(
        settings,
        workspace_id="ws_from_token",
        token_workspace_id="ws_from_token",
    )

    assert token == "xoxb-local"
    assert client is None

    with pytest.raises(ValueError, match="workspace_scope_mismatch"):
        _resolve_slack_tool_access(
            settings,
            workspace_id="ws_other",
            token_workspace_id="ws_from_token",
        )


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_auto_selects_single_connected_cluster() -> None:
    client = FakeAgentClusterClient(
        [{"cluster_id": "cluster_one", "name": "prod", "connected": True}]
    )

    cluster_id = await _resolve_k8s_cluster_id(client=client, bearer_token="token")

    assert cluster_id == "cluster_one"


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_rejects_ambiguous_connected_clusters() -> None:
    client = FakeAgentClusterClient(
        [
            {"cluster_id": "cluster_prod", "name": "prod", "connected": True},
            {"cluster_id": "cluster_stage", "name": "stage", "connected": True},
        ]
    )

    with pytest.raises(ValueError, match="Multiple Kubernetes clusters"):
        await _resolve_k8s_cluster_id(client=client, bearer_token="token")


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_environment_aliases() -> None:
    client = FakeAgentClusterClient(
        [
            {
                "cluster_id": "cluster_prod",
                "name": "prod-us-east-1",
                "environment": "production",
                "connected": True,
            },
            {
                "cluster_id": "cluster_stage",
                "name": "staging-eu",
                "environment": "staging",
                "aliases": ["stage"],
                "connected": True,
            },
        ]
    )

    assert (
        await _resolve_k8s_cluster_id(
            client=client,
            bearer_token="token",
            environment="prod",
        )
        == "cluster_prod"
    )
    assert (
        await _resolve_k8s_cluster_id(
            client=client,
            bearer_token="token",
            environment="stage",
        )
        == "cluster_stage"
    )


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_name_matches_alias() -> None:
    client = FakeAgentClusterClient(
        [
            {
                "cluster_id": "cluster_prod",
                "name": "prod-us-east-1",
                "aliases": ["primary"],
                "connected": True,
            }
        ]
    )

    cluster_id = await _resolve_k8s_cluster_id(
        client=client,
        bearer_token="token",
        cluster_name="primary",
    )

    assert cluster_id == "cluster_prod"


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_explicit_cluster_id_bypasses_lookup() -> None:
    client = FakeAgentClusterClient([])

    cluster_id = await _resolve_k8s_cluster_id(
        client=client,
        bearer_token="token",
        cluster_id="cluster_debug",
    )

    assert cluster_id == "cluster_debug"
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_resolve_k8s_cluster_no_connected_clusters_is_helpful() -> None:
    client = FakeAgentClusterClient([])

    with pytest.raises(ValueError, match="No Kubernetes cluster is connected"):
        await _resolve_k8s_cluster_id(client=client, bearer_token="token")


@pytest.mark.asyncio
async def test_k8s_connection_health_reports_connected_and_permissions() -> None:
    client = FakeK8sHealthClient(
        [
            {
                "cluster_id": "cluster_prod",
                "name": "incidentflow-prod",
                "connected": True,
                "agent_version": "0.1.0",
            }
        ],
        {
            ("k8s.list_namespaces", ""): {
                "status": "succeeded",
                "data": {"namespaces": [{"name": "incidentflow-prod"}]},
            },
            ("k8s.list_pods", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {
                    "pods": [
                        {
                            "name": "api-1",
                            "namespace": "incidentflow-prod",
                            "phase": "Running",
                            "containers": [{"ready": True, "restart_count": 0}],
                        }
                    ]
                },
            },
            ("k8s.list_events", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"events": []},
            },
            ("k8s.list_deployments", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"deployments": []},
            },
            ("k8s.list_services", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"services": []},
            },
            ("k8s.get_pod_logs", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"logs": "ok"},
            },
        },
    )

    payload = await _k8s_connection_health_payload(client=client, bearer_token="token")

    assert payload["status"] == "connected"
    assert payload["agent_online"] is True
    assert payload["agent_version"] == "0.1.0"
    assert payload["namespaces"] == ["incidentflow-prod"]
    assert payload["permissions"]["get_logs"] is True


@pytest.mark.asyncio
async def test_k8s_connection_health_leaves_logs_permission_unknown_without_pods() -> None:
    client = FakeK8sHealthClient(
        [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}],
        {
            ("k8s.list_namespaces", ""): {
                "status": "succeeded",
                "data": {"namespaces": [{"name": "redis"}]},
            },
            ("k8s.list_pods", "redis"): {"status": "succeeded", "data": {"pods": []}},
            ("k8s.list_events", "redis"): {"status": "succeeded", "data": {"events": []}},
            ("k8s.list_deployments", "redis"): {
                "status": "succeeded",
                "data": {"deployments": []},
            },
            ("k8s.list_services", "redis"): {"status": "succeeded", "data": {"services": []}},
        },
    )

    payload = await _k8s_connection_health_payload(client=client, bearer_token="token")

    assert payload["permissions"]["get_logs"] is None


@pytest.mark.asyncio
async def test_k8s_connection_health_reports_degraded_when_dispatch_fails() -> None:
    client = FailingK8sDispatchClient(
        [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}]
    )

    payload = await _k8s_connection_health_payload(client=client, bearer_token="token")

    assert payload["status"] == "degraded"
    assert payload["permissions"]["list_namespaces"] is False
    assert payload["namespaces"] == []


@pytest.mark.asyncio
async def test_k8s_rbac_check_reports_denied_action() -> None:
    client = FakeK8sHealthClient(
        [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}],
        {
            ("k8s.list_namespaces", ""): {
                "status": "succeeded",
                "data": {"namespaces": [{"name": "incidentflow-prod"}]},
            },
            ("k8s.list_pods", "incidentflow-prod"): {
                "status": "failed",
                "error": {"code": "RBAC_DENIED", "message": "denied"},
            },
            ("k8s.list_events", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"events": []},
            },
            ("k8s.list_deployments", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"deployments": []},
            },
            ("k8s.list_services", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"services": []},
            },
        },
    )

    payload = await _k8s_rbac_check_payload(
        client=client,
        bearer_token="token",
        cluster_id="cluster_prod",
    )

    assert payload["permissions"]["list_pods"]["allowed"] is False
    assert payload["permissions"]["list_pods"]["error_code"] == "RBAC_DENIED"
    assert payload["permissions"]["get_logs"]["allowed"] is None


@pytest.mark.asyncio
async def test_k8s_cluster_overview_aggregates_unhealthy_pods_and_restarts() -> None:
    client = FakeK8sHealthClient(
        [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}],
        {
            ("k8s.list_namespaces", ""): {
                "status": "succeeded",
                "data": {"namespaces": [{"name": "incidentflow-prod"}]},
            },
            ("k8s.list_pods", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {
                    "pods": [
                        {
                            "name": "api-1",
                            "namespace": "incidentflow-prod",
                            "phase": "Running",
                            "containers": [{"ready": True, "restart_count": 0}],
                        },
                        {
                            "name": "worker-1",
                            "namespace": "incidentflow-prod",
                            "phase": "Running",
                            "containers": [{"ready": False, "restart_count": 5}],
                        },
                    ]
                },
            },
            ("k8s.list_deployments", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"deployments": [{"name": "api"}]},
            },
            ("k8s.list_services", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {"services": [{"name": "api"}]},
            },
            ("k8s.list_events", "incidentflow-prod"): {
                "status": "succeeded",
                "data": {
                    "events": [
                        {
                            "type": "Warning",
                            "reason": "BackOff",
                            "last_seen": "2026-06-21T00:00:00Z",
                        }
                    ]
                },
            },
        },
    )

    payload = await _k8s_cluster_overview_payload(
        client=client,
        bearer_token="token",
        cluster_id="cluster_prod",
    )

    assert payload["pods_total"] == 2
    assert payload["pods_unhealthy"] == 1
    assert payload["deployments"] == 1
    assert payload["services"] == 1
    assert payload["recent_warning_events"] == 1
    assert payload["top_restarts"][0]["pod"] == "worker-1"


@pytest.mark.asyncio
async def test_k8s_cluster_overview_excludes_succeeded_pods_from_unhealthy() -> None:
    client = FakeK8sHealthClient(
        [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}],
        {
            ("k8s.list_namespaces", ""): {
                "status": "succeeded",
                "data": {"namespaces": [{"name": "jobs"}]},
            },
            ("k8s.list_pods", "jobs"): {
                "status": "succeeded",
                "data": {
                    "pods": [
                        {
                            "name": "migration-job",
                            "namespace": "jobs",
                            "phase": "Succeeded",
                            "containers": [{"name": "main", "ready": False}],
                        }
                    ]
                },
            },
            ("k8s.list_deployments", "jobs"): {
                "status": "succeeded",
                "data": {"deployments": []},
            },
            ("k8s.list_services", "jobs"): {"status": "succeeded", "data": {"services": []}},
            ("k8s.list_events", "jobs"): {"status": "succeeded", "data": {"events": []}},
        },
    )

    payload = await _k8s_cluster_overview_payload(
        client=client,
        bearer_token="token",
        cluster_id="cluster_prod",
    )

    assert payload["pods_unhealthy"] == 0
    assert payload["completed_jobs"][0]["pod"] == "migration-job"


def test_filter_workload_pods_uses_deployment_selector() -> None:
    pods = [
        {"name": "api-abc", "labels": {"app": "api", "pod-template-hash": "abc"}},
        {"name": "worker-abc", "labels": {"app": "worker"}},
    ]
    deployments = [{"name": "api", "selector": {"app": "api"}}]

    assert [pod["name"] for pod in _filter_workload_pods(pods, deployments, "api")] == ["api-abc"]


def test_overview_downgrades_stale_warning_for_ready_pod() -> None:
    last_seen = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    payload = _overview_payload(
        namespaces=["incidentflow-dev"],
        pods=[
            {
                "name": "incidentflow-mcp-abc",
                "namespace": "incidentflow-dev",
                "phase": "Running",
                "containers": [{"ready": True, "restart_count": 0}],
            }
        ],
        deployments=[],
        services=[],
        events=[
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "object": "pod/incidentflow-mcp-abc",
                "last_seen": last_seen,
            }
        ],
        namespace="incidentflow-dev",
    )

    assert payload["pods_unhealthy"] == 0
    assert payload["warning_event_summary"]["active_warning_events"] == 0
    assert payload["warning_event_summary"]["stale_rollout_warning_events"] == 1


def test_overview_downgrades_stale_warning_for_replaced_pod() -> None:
    last_seen = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    payload = _overview_payload(
        namespaces=["incidentflow-dev"],
        pods=[
            {
                "name": "incidentflow-mcp-new",
                "namespace": "incidentflow-dev",
                "phase": "Running",
                "containers": [{"ready": True, "restart_count": 0}],
            }
        ],
        deployments=[],
        services=[],
        events=[
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "object": "Pod/incidentflow-mcp-old",
                "last_seen": last_seen,
            }
        ],
        namespace="incidentflow-dev",
    )

    stale_example = payload["warning_event_summary"]["stale_examples"][0]
    assert payload["warning_event_summary"]["active_warning_events"] == 0
    assert payload["warning_event_summary"]["stale_rollout_warning_events"] == 1
    assert stale_example["pod"] == "incidentflow-mcp-old"
    assert stale_example["pod_exists"] is False


def test_overview_treats_single_restart_ready_pod_as_healthy() -> None:
    payload = _overview_payload(
        namespaces=["observability"],
        pods=[
            {
                "name": "prometheus-server-abc",
                "namespace": "observability",
                "phase": "Running",
                "containers": [{"ready": True, "restart_count": 1}],
            }
        ],
        deployments=[],
        services=[],
        events=[],
        namespace="observability",
    )

    assert payload["pods_unhealthy"] == 0
    assert payload["top_restarts"] == [
        {
            "namespace": "observability",
            "pod": "prometheus-server-abc",
            "phase": "Running",
            "node": None,
            "restarts": 1,
            "last_restart_at": None,
            "restarts_last_1h": 0,
            "restarts_last_24h": 0,
        }
    ]


def test_cluster_health_assessment_deduplicates_warning_recommendations() -> None:
    health = _cluster_health_assessment(
        {
            "pods_total": 3,
            "pods_unhealthy": 0,
            "warning_event_summary": {"active_warning_events": 1},
            "top_restarts": [],
        }
    )

    assert health["cluster_health"] == "Warning"
    assert health["recommendations"] == ["Review active warning events with k8s_list_events"]


@pytest.mark.asyncio
async def test_k8s_namespace_overview_returns_namespace_error_before_empty_overview(
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

    async def allow_tool(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    async def list_clusters(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
    ) -> list[dict[str, object]]:
        _ = self
        assert bearer_token == "token"
        return [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}]

    calls: list[tuple[str, dict[str, object]]] = []

    async def send_agent_command(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        _ = self, timeout_seconds
        assert bearer_token == "token"
        assert cluster_id == "cluster_prod"
        calls.append((action, params))
        if action == "k8s.list_pods":
            return {
                "command_id": "cmd_ns",
                "status": "failed",
                "data": None,
                "error": {
                    "code": "NAMESPACE_DENIED",
                    "message": 'namespace "does-not-exist" is not allowed',
                },
            }
        raise AssertionError(f"unexpected action after namespace preflight: {action}")

    monkeypatch.setattr(
        "incidentflow_mcp.mcp.server.resolve_tool_integration_context",
        allow_tool,
    )
    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "list_clusters", list_clusters)
    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "send_agent_command", send_agent_command)

    _set_k8s_tool_context()
    try:
        result = await create_mcp_server()._tool_manager.call_tool(
            "k8s_namespace_overview",
            {"namespace": "does-not-exist"},
        )
    finally:
        clear_current_auth_context()

    assert result["status"] == "failed"
    assert result["error"]["code"] == "NAMESPACE_DENIED"
    assert result["cluster_id"] == "cluster_prod"
    assert calls == [("k8s.list_pods", {"namespace": "does-not-exist"})]


@pytest.mark.asyncio
async def test_k8s_analyze_workload_missing_workload_is_not_healthy(
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

    async def allow_tool(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    async def list_clusters(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
    ) -> list[dict[str, object]]:
        _ = self
        assert bearer_token == "token"
        return [{"cluster_id": "cluster_prod", "name": "prod", "connected": True}]

    async def send_agent_command(
        self: PlatformAPIAgentCommandsClient,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        _ = self, timeout_seconds
        assert bearer_token == "token"
        assert cluster_id == "cluster_prod"
        assert params.get("namespace") == "incidentflow-dev"
        if action == "k8s.get_rollout_status":
            return {
                "command_id": "cmd_rollout",
                "status": "failed",
                "data": None,
                "error": {
                    "code": "NOT_FOUND",
                    "message": 'deployments.apps "does-not-exist" not found',
                },
            }
        if action == "k8s.list_pods":
            return {"command_id": "cmd_pods", "status": "succeeded", "data": {"pods": []}}
        if action == "k8s.list_deployments":
            return {
                "command_id": "cmd_deployments",
                "status": "succeeded",
                "data": {"deployments": []},
            }
        raise AssertionError(f"unexpected action for missing workload: {action}")

    monkeypatch.setattr(
        "incidentflow_mcp.mcp.server.resolve_tool_integration_context",
        allow_tool,
    )
    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "list_clusters", list_clusters)
    monkeypatch.setattr(PlatformAPIAgentCommandsClient, "send_agent_command", send_agent_command)

    _set_k8s_tool_context()
    try:
        result = await create_mcp_server()._tool_manager.call_tool(
            "k8s_analyze_workload",
            {"namespace": "incidentflow-dev", "workload": "does-not-exist"},
        )
    finally:
        clear_current_auth_context()

    assert result["status"] == "failed"
    assert result["health"] == "unknown"
    assert result["severity"] == "warning"
    assert result["summary"] == "No matching workload found for does-not-exist"
    assert result["error"]["code"] == "NOT_FOUND"
    assert result["data"]["pods_total"] == 0


def test_compact_log_payload_filters_noise_and_highlights_errors() -> None:
    payload = {
        "status": "succeeded",
        "data": {
            "logs": "\n".join(
                [
                    "DEBUG httpcore.connection noise",
                    "INFO started",
                    "ERROR timeout talking to db",
                ]
            )
        },
    }

    compact = _compact_log_payload(
        payload,
        level=None,
        contains=None,
        exclude=None,
        compact=True,
    )

    assert compact["data"]["skipped_debug_lines"] == 1
    assert compact["data"]["highlighted"] == ["ERROR timeout talking to db"]


def test_compact_log_payload_count_metadata_matches_returned_lines_after_filtering() -> None:
    payload = {
        "status": "succeeded",
        "data": {
            "logs": "\n".join(
                [
                    "DEBUG httpcore.connection noise",
                    "INFO started",
                    "DEBUG httpx noise",
                    "WARNING dependency timeout",
                ]
            )
        },
    }

    compact = _compact_log_payload(
        payload,
        level=None,
        contains=None,
        exclude=None,
        compact=True,
    )

    assert compact["data"]["line_count"] == 4
    assert compact["data"]["skipped_debug_lines"] == 2
    assert compact["data"]["returned_line_count"] == len(compact["data"]["lines"]) == 2


def test_compact_log_payload_redacts_secrets() -> None:
    payload = {
        "status": "succeeded",
        "data": {"logs": "INFO redis_url=redis://:super-secret@redis-master:6379/0 token=abc123"},
    }

    compact = _compact_log_payload(
        payload,
        level=None,
        contains=None,
        exclude=None,
        compact=True,
    )

    assert compact["data"]["lines"] == ["INFO redis_url=redis://***@redis-master:6379/0 token=***"]


def test_compact_log_payload_marks_truncated_when_compact_cap_applies() -> None:
    payload = {
        "status": "succeeded",
        "data": {"logs": "\n".join(f"INFO line {idx}" for idx in range(121))},
    }

    compact = _compact_log_payload(
        payload,
        level=None,
        contains=None,
        exclude=None,
        compact=True,
    )

    assert compact["truncated"] is True
    assert compact["data"]["truncated"] is True
    assert compact["data"]["line_count"] == 121
    assert compact["data"]["returned_line_count"] == 120
    assert len(compact["data"]["lines"]) == 120


def test_analyze_workload_logs_summarizes_and_redacts_internal_details() -> None:
    analysis = _analyze_workload_logs(
        {
            "line_count": 5,
            "skipped_debug_lines": 2,
            "lines": [
                (
                    "INFO event='agent command dispatch completed' "
                    "duration_ms=123 workspace_id=ws_123 agent_id=agent_456 "
                    "url=http://incidentflow-agent-gateway.incidentflow-dev.svc.cluster.local"
                    "/internal/agents/commands/dispatch"
                ),
                "DEBUG httpcore.connection connect_tcp.started host='10.0.0.1'",
                "WARNING dependency timeout duration_ms=164",
                "INFO GET /api/health status_code=200 duration_ms=12",
                "ERROR failed dependency request_id=req_123",
            ],
        },
        exclude_loggers=["httpcore.*"],
    )

    assert analysis["lines_scanned"] == 5
    assert analysis["errors"] == 1
    assert analysis["warnings"] == 1
    assert analysis["latency"] == {"p50_ms": 123.0, "max_ms": 164.0}
    assert analysis["log_categories"]["internal_debug"] == 3
    assert analysis["log_categories"]["http_access"] == 1
    assert analysis["log_categories"]["dependency"] == 2
    assert analysis["top_patterns"][0] == {
        "event": "agent command dispatch completed",
        "count": 1,
    }
    assert all("svc.cluster.local" not in line for line in analysis["notable_lines"])
    assert all("/internal/agents" not in line for line in analysis["notable_lines"])


def test_diagnose_pod_treats_ready_pod_probe_events_as_historical() -> None:
    diagnosis = _diagnose_pod(
        {
            "phase": "Running",
            "containers": [{"name": "api", "ready": True, "restart_count": 0}],
        },
        [
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "message": "Startup probe failed during startup",
            }
        ],
    )

    assert diagnosis["healthy"] is True
    assert diagnosis["issues"] == []
    assert diagnosis["historical_warnings"] == ["StartupProbeFailure"]


def test_describe_pod_structured_reports_historical_restart_as_observation() -> None:
    response = _describe_pod_structured(
        {
            "name": "api-123",
            "namespace": "incidentflow-dev",
            "phase": "Running",
            "age": "25d",
            "containers": [{"name": "api", "ready": True, "restart_count": 1}],
        },
        [],
    )

    assert response["data"]["diagnosis"]["healthy"] is True
    assert response["data"]["diagnosis"]["issues"] == []
    assert response["observations"] == [
        {
            "severity": "info",
            "code": "HISTORICAL_RESTART",
            "message": "Container restarted 1 time during the pod lifetime",
            "count": 1,
        }
    ]
    assert response["data"]["observations"] == response["observations"]
    assert response["recommendations"] == [
        (
            "No immediate action required. Check the previous container termination reason "
            "if the restart was recent or recurring."
        )
    ]
    assert response["next_actions"] == [
        {
            "action": "k8s_describe_pod",
            "priority": "low",
            "reason": "Determine the historical restart cause",
            "tool_arguments": {
                "namespace": "incidentflow-dev",
                "pod": "api-123",
            },
        }
    ]
    assert response["data"]["next_actions"] == response["next_actions"]


def test_restart_window_summary_uses_last_restart_timestamp() -> None:
    summary = _restart_window_summary(
        [
            {
                "name": "api",
                "restart_count": 2,
                "last_restart_at": "2026-07-19T15:30:00Z",
            },
            {
                "name": "worker",
                "restart_count": 1,
                "last_state": {
                    "terminated": {
                        "finished_at": "2026-07-18T14:00:00Z",
                    }
                },
            },
        ],
        now=datetime(2026, 7, 19, 16, 0, tzinfo=UTC),
    )

    assert summary == {
        "last_restart_at": "2026-07-19T15:30:00Z",
        "restarts_last_1h": 2,
        "restarts_last_24h": 2,
    }


def test_build_describe_response_keeps_running_ready_restart_healthy() -> None:
    response = _build_describe_response(
        {
            "metadata": {
                "name": "prometheus-server-abc",
                "namespace": "observability",
                "age": "25d",
            },
            "status": {
                "phase": "Running",
                "ready": True,
            },
            "containers": [
                {
                    "name": "prometheus-server",
                    "ready": True,
                    "restart_count": 1,
                    "last_restart_at": "2000-01-01T00:00:00Z",
                    "last_state": {
                        "terminated": {
                            "exit_code": 137,
                            "reason": "Error",
                            "finished_at": "2000-01-01T00:00:00Z",
                        }
                    },
                },
                {"name": "config-reload", "ready": True, "restart_count": 0},
            ],
            "resources": {
                "qos_class": "Burstable",
                "containers": [
                    {"name": "prometheus-server", "requests": {}, "limits": {}},
                    {"name": "config-reload", "requests": {"cpu": "10m"}, "limits": {}},
                ],
            },
            "events": [],
        },
        include_details=False,
    )

    assert response["data"]["diagnosis"]["healthy"] is True
    assert response["data"]["diagnosis"]["current_issues"] == []
    assert response["data"]["diagnosis"]["historical_warnings"] == [
        {
            "severity": "info",
            "type": "PreviousContainerTermination",
            "container": "prometheus-server",
            "exit_code": 137,
            "reason": "Error",
            "finished_at": "2000-01-01T00:00:00Z",
            "message": (
                "Container was previously terminated with exit code 137. "
                "The pod has remained healthy since restart."
            ),
        }
    ]
    assert response["data"]["status"]["restart_count"] == 1
    assert response["data"]["status"]["last_restart_at"] == "2000-01-01T00:00:00Z"
    assert response["data"]["status"]["restarts_last_1h"] == 0
    assert response["data"]["status"]["restarts_last_24h"] == 0
    assert response["data"]["containers"][0]["last_restart_at"] == "2000-01-01T00:00:00Z"
    assert response["observations"][0]["code"] == "HISTORICAL_RESTART"
    assert response["observations"][0]["last_restart_at"] == "2000-01-01T00:00:00Z"
    assert response["observations"][1] == {
        "severity": "info",
        "code": "PreviousContainerTermination",
        "message": (
            "Container was previously terminated with exit code 137. "
            "The pod has remained healthy since restart."
        ),
        "container": "prometheus-server",
        "exit_code": 137,
        "reason": "Error",
        "finished_at": "2000-01-01T00:00:00Z",
    }
    assert (
        "⚠ prometheus-server has no explicit CPU or memory requests/limits" in response["findings"]
    )
    assert response["recommendations"] == [
        (
            "No immediate action required. Check the previous container termination reason "
            "if the restart was recent or recurring."
        )
    ]
    assert response["next_actions"] == [
        {
            "action": "k8s_get_pod_logs",
            "priority": "low",
            "reason": "Inspect recent logs only if the restart was recent or recurring",
            "tool_arguments": {
                "namespace": "observability",
                "pod": "prometheus-server-abc",
                "tail_lines": 100,
            },
        }
    ]


def test_structured_tool_exception_wraps_http_status_body() -> None:
    request = httpx.Request("POST", "https://platform.example/internal")
    response = httpx.Response(
        403,
        request=request,
        json={"error": {"code": "FORBIDDEN", "message": "Dashboard is not approved"}},
    )
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)

    payload = _structured_tool_exception(exc, code="GRAFANA_HTTP_ERROR")

    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "HTTP_403"
    assert payload["error"]["http_status"] == 403
    assert payload["error"]["upstream_response"]["error"]["code"] == "FORBIDDEN"


async def test_fastmcp_unknown_tool_arguments_return_structured_validation_error() -> None:
    from incidentflow_mcp.mcp.server import create_mcp_server

    mcp = create_mcp_server()
    result = await mcp._tool_manager.call_tool(
        "k8s_get_pod",
        {"namespace": "default", "pod": "api-123", "tail_lines_typo": 10},
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["error"]["details"][0]["type"] == "extra_forbidden"
    assert result["error"]["details"][0]["loc"] == ("tail_lines_typo",)


def test_compact_external_status_includes_failed_provider_entries() -> None:
    result = _compact_external_status_result(
        {
            "status": "partial",
            "external_status": [
                {
                    "provider": "github",
                    "indicator": "none",
                    "description": "All Systems Operational",
                    "incidents": [],
                    "degraded_components": [],
                    "fetched_at": "2026-06-25T10:00:00Z",
                }
            ],
            "errors": [
                {
                    "provider": "aws",
                    "message": "AWS status fetch failed",
                    "error_type": "RuntimeError",
                    "source_url": "https://status.aws.amazon.com/rss/all.rss",
                    "status_code": 503,
                }
            ],
        }
    )

    assert result["status"] == "partial"
    assert result["providers"][1]["provider"] == "aws"
    assert result["providers"][1]["status"] == "error"
    assert result["providers"][1]["source_url"] == "https://status.aws.amazon.com/rss/all.rss"


def test_compact_external_status_limits_historical_incidents() -> None:
    incidents = [
        {
            "name": f"incident-{index}",
            "status": "resolved",
            "created_at": f"2026-06-{index + 1:02d}T00:00:00Z",
        }
        for index in range(8)
    ]

    result = _compact_external_status_result(
        {
            "status": "success",
            "external_status": [
                {
                    "provider": "github",
                    "indicator": "none",
                    "description": "ok",
                    "incidents": incidents,
                    "degraded_components": [],
                    "fetched_at": "2026-06-25T10:00:00Z",
                }
            ],
            "errors": [],
        }
    )

    provider = result["providers"][0]
    assert len(provider["historical_incidents"]) == 5
    assert provider["historical_incidents_total"] == 8
    assert provider["truncated"] is True


@pytest.mark.asyncio
async def test_platform_api_jobs_client_submit_includes_internal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self._timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict, headers: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured["key"] = headers.get("X-Internal-Api-Key", "")
            captured["job_type"] = json["job_type"]
            return FakeResponse(
                {
                    "job_id": "job_123",
                    "status": "queued",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )

    monkeypatch.setattr(
        "incidentflow_mcp.platform_api.ai_jobs_client.httpx.AsyncClient",
        FakeAsyncClient,
    )

    settings = Settings(
        _env_file=None,
        environment="test",
        platform_api_base_url="http://platform.test",
        platform_api_internal_api_key="secret-key",
    )
    client = PlatformAPIJobsClient(settings)
    payload = {"job_type": "incident.summary.generate"}
    response = await client.submit_job(payload)

    assert response["job_id"] == "job_123"
    assert captured["url"] == "http://platform.test/api/v1/ai/jobs"
    assert captured["key"] == "secret-key"
    assert captured["job_type"] == "incident.summary.generate"


def test_normalize_polled_incident_summary_job_running_returns_async() -> None:
    output = _normalize_polled_incident_summary_job(
        job_id="sum_1",
        job={"status": "running"},
        poll_after_seconds=2,
    )
    payload = _payload(output)

    assert payload["mode"] == "async"
    assert payload["job_id"] == "sum_1"
    assert payload["status"] == "running"
    assert payload["poll_after_seconds"] == 2


def test_normalize_polled_incident_summary_job_terminal_returns_completed_payload() -> None:
    output = _normalize_polled_incident_summary_job(
        job_id="sum_2",
        job={
            "status": "succeeded",
            "result": {"title": "DB outage", "severity": "sev1"},
            "artifact_refs": ["artifact_1", "mock_ref"],
            "usage": {"tokens": 42},
            "updated_at": "2026-07-08T18:00:00Z",
        },
        poll_after_seconds=2,
    )
    payload = _payload(output)

    assert payload["mode"] == "completed"
    assert payload["job_id"] == "sum_2"
    assert payload["status"] == "succeeded"
    assert payload["result"] == {"title": "DB outage", "severity": "sev1"}
    assert payload["artifact_refs"] == ["artifact_1"]
    assert payload["usage"] == {"tokens": 42}
    assert payload["updated_at"] == "2026-07-08T18:00:00Z"


def test_normalize_polled_incident_summary_job_rejects_external_status_result() -> None:
    output = _normalize_polled_incident_summary_job(
        job_id="sum_wrong",
        job={
            "status": "succeeded",
            "result": {
                "action": "fetched_external_status",
                "external_status": [{"provider": "github"}],
            },
            "artifact_refs": ["mock_ref"],
        },
        poll_after_seconds=2,
    )
    payload = _payload(output)

    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "JOB_OPERATION_MISMATCH"
    assert "result" not in payload
    assert "artifact_refs" not in payload


def test_normalize_polled_incident_summary_job_rejects_wrong_job_type() -> None:
    output = _normalize_polled_incident_summary_job(
        job_id="sum_wrong_type",
        job={
            "status": "succeeded",
            "job_type": "alert.group.summary.generate",
            "result": {"title": "Should not leak"},
        },
        poll_after_seconds=2,
    )
    payload = _payload(output)

    assert payload["status"] == "failed"
    assert payload["error"]["expected_job_type"] == "incident.summary.generate"
    assert "Should not leak" not in json.dumps(payload)


def test_normalize_polled_incident_summary_job_failed_returns_error() -> None:
    output = _normalize_polled_incident_summary_job(
        job_id="sum_3",
        job={"status": "failed", "error": "runner crashed"},
        poll_after_seconds=2,
    )
    payload = _payload(output)

    assert payload["mode"] == "completed"
    assert payload["status"] == "failed"
    assert payload["error"] == "runner crashed"


def test_normalize_polled_external_status_job_running_returns_async() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_1",
        job={"status": "running"},
        poll_after_seconds=2,
        response_mode="compact",
    )
    payload = _payload(output)

    assert payload["mode"] == "async"
    assert payload["job_id"] == "job_1"
    assert payload["status"] == "running"
    assert payload["poll_after_seconds"] == 2


def test_normalize_polled_external_status_job_terminal_returns_compact_payload() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_2",
        job={
            "status": "succeeded",
            "result": {
                "status": "success",
                "action": "fetched_external_status",
                "providers_succeeded": 1,
                "external_status": [
                    {
                        "provider": "github",
                        "indicator": "minor",
                        "description": "Degraded",
                        "fetched_at": "2026-03-17T18:00:00Z",
                        "incidents": [
                            {
                                "id": "inc_1",
                                "name": "Incident 1",
                                "status": "investigating",
                                "impact": "minor",
                                "created_at": "2026-03-17T00:00:00Z",
                                "updated_at": "2026-03-17T00:10:00Z",
                                "shortlink": "https://status/1",
                                "incident_updates": [
                                    {
                                        "status": "investigating",
                                        "body": "very large payload",
                                        "created_at": "2026-03-17T00:10:00Z",
                                    }
                                ],
                            }
                        ],
                        "degraded_components": [
                            {"name": "Actions", "status": "degraded_performance"}
                        ],
                        "regional_status_errors": {"eu": "404 Not Found"},
                    }
                ],
            },
            "artifact_refs": ["artifact_1"],
            "usage": {"tokens": 1},
            "updated_at": "2026-03-17T18:00:00Z",
        },
        poll_after_seconds=2,
        response_mode="compact",
    )
    payload = _payload(output)

    assert payload["status"] == "ok"
    assert payload["checked_at"] == "2026-03-17T18:00:00Z"
    provider = payload["providers"][0]
    compact_incident = provider["active_incidents"][0]
    assert compact_incident["id"] == "inc_1"
    assert compact_incident["updates_count"] == 1
    assert compact_incident["latest_update_status"] == "investigating"
    assert compact_incident["latest_update_at"] == "2026-03-17T00:10:00Z"
    assert "incident_updates" not in compact_incident
    assert provider["regional_status_errors"] == {"eu": "404 Not Found"}


def test_compact_external_status_uses_latest_update_timestamp_not_list_order() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_123",
        job={
            "id": "job_123",
            "status": "succeeded",
            "result": {
                "status": "success",
                "external_status": [
                    {
                        "provider": "github",
                        "indicator": "none",
                        "description": "All Systems Operational",
                        "fetched_at": "2026-03-17T18:00:00Z",
                        "incidents": [
                            {
                                "id": "inc_1",
                                "name": "Incident 1",
                                "status": "resolved",
                                "incident_updates": [
                                    {
                                        "status": "resolved",
                                        "created_at": "2026-03-17T00:30:00Z",
                                    },
                                    {
                                        "status": "monitoring",
                                        "created_at": "2026-03-17T00:20:00Z",
                                    },
                                    {
                                        "status": "investigating",
                                        "created_at": "2026-03-17T00:10:00Z",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        poll_after_seconds=2,
        response_mode="compact",
    )

    incident = _payload(output)["providers"][0]["historical_incidents"][0]
    assert incident["latest_update_status"] == "resolved"
    assert incident["latest_update_at"] == "2026-03-17T00:30:00Z"


def test_normalize_polled_external_status_job_terminal_returns_full_payload() -> None:
    raw_result = {
        "status": "success",
        "external_status": [
            {
                "provider": "github",
                "incidents": [
                    {
                        "id": "inc_1",
                        "name": "Incident 1",
                        "incident_updates": [{"body": "full payload"}],
                    }
                ],
            }
        ],
    }

    output = _normalize_polled_external_status_job(
        job_id="job_2",
        job={"status": "succeeded", "result": raw_result},
        poll_after_seconds=2,
        response_mode="full",
    )
    payload = _payload(output)

    assert payload["mode"] == "completed"
    assert payload["response_mode"] == "full"
    assert payload["result"] == raw_result


@pytest.mark.asyncio
async def test_external_status_check_starts_new_job_when_check_id_missing() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.get_calls = 0

        async def submit_job(self, payload: dict) -> dict:
            self.submit_calls += 1
            assert payload["job_type"] == "alert.group.summary.generate"
            assert payload["payload"]["providers"] == ["aws"]
            return {"job_id": "new_job", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:
            self.get_calls += 1
            return {"job_id": job_id, "status": "running"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["aws"],
        workspace_id="ws_1",
        check_id=None,
        wait_for_result=False,
        response_mode="compact",
    )
    payload = _payload(output)

    assert fake_client.submit_calls == 1
    assert fake_client.get_calls == 0
    assert payload["mode"] == "async"
    assert payload["job_id"] == "new_job"
    assert payload["status"] == "queued"


@pytest.mark.asyncio
async def test_external_status_check_polls_existing_job_when_check_id_present() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.get_calls = 0

        async def submit_job(self, payload: dict) -> dict:
            self.submit_calls += 1
            return {"job_id": "unused", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:
            self.get_calls += 1
            assert job_id == "existing_job"
            return {
                "job_id": job_id,
                "status": "failed",
                "error": {"category": "retryable", "reason": "provider timeout"},
            }

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["aws", "github"],
        workspace_id="ws_1",
        check_id="existing_job",
        response_mode="compact",
    )
    payload = _payload(output)

    assert fake_client.submit_calls == 0
    assert fake_client.get_calls == 1
    assert payload["mode"] == "completed"
    assert payload["job_id"] == "existing_job"
    assert payload["status"] == "failed"
    assert payload["error"]["reason"] == "provider timeout"
    assert payload["response_mode"] == "compact"


@pytest.mark.asyncio
async def test_external_status_check_rejects_missing_workspace_scope() -> None:
    class FakeClient:
        async def submit_job(
            self, payload: dict
        ) -> dict:  # pragma: no cover - should not be called
            return {"job_id": "unused", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:  # pragma: no cover - should not be called
            return {"job_id": job_id, "status": "queued"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )
    with pytest.raises(ValueError):
        await _execute_external_status_check(
            settings=settings,
            client=FakeClient(),
            providers=["aws"],
            workspace_id=None,
            check_id=None,
            wait_for_result=False,
            response_mode="compact",
        )


@pytest.mark.asyncio
async def test_external_status_check_uses_default_workspace_when_omitted() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.payload: dict | None = None

        async def submit_job(self, payload: dict) -> dict:
            self.payload = payload
            return {"job_id": "job_default_ws", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:  # pragma: no cover - should not be called
            return {"job_id": job_id, "status": "queued"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
        mcp_default_workspace_id="35b02121-716b-4097-a851-84485d39b76f",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["github"],
        workspace_id=None,
        check_id=None,
        wait_for_result=False,
        response_mode="compact",
    )
    payload = _payload(output)

    assert fake_client.payload is not None
    assert fake_client.payload["workspace_id"] == "35b02121-716b-4097-a851-84485d39b76f"
    assert payload["mode"] == "async"
    assert payload["job_id"] == "job_default_ws"


@pytest.mark.asyncio
async def test_external_status_check_uses_token_workspace_when_omitted() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.payload: dict | None = None

        async def submit_job(self, payload: dict) -> dict:
            self.payload = payload
            return {"job_id": "job_token_ws", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:  # pragma: no cover - should not be called
            return {"job_id": job_id, "status": "queued"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
        mcp_default_workspace_id="35b02121-716b-4097-a851-84485d39b76f",
    )
    fake_client = FakeClient()

    output = await _execute_external_status_check(
        settings=settings,
        client=fake_client,
        providers=["github"],
        workspace_id=None,
        check_id=None,
        wait_for_result=False,
        response_mode="compact",
        token_workspace_id="7b6f0f1d-8e89-4a53-85ef-bc2e4cd9ba9b",
    )
    payload = _payload(output)

    assert fake_client.payload is not None
    assert fake_client.payload["workspace_id"] == "7b6f0f1d-8e89-4a53-85ef-bc2e4cd9ba9b"
    assert payload["job_id"] == "job_token_ws"


@pytest.mark.asyncio
async def test_external_status_check_rejects_explicit_workspace_scope_mismatch() -> None:
    class FakeClient:
        async def submit_job(
            self, payload: dict
        ) -> dict:  # pragma: no cover - should not be called
            return {"job_id": "unused", "status": "queued"}

        async def get_job(self, job_id: str) -> dict:  # pragma: no cover - should not be called
            return {"job_id": job_id, "status": "queued"}

    settings = Settings(
        _env_file=None,
        environment="development",
        platform_api_base_url="http://platform.test",
    )

    with pytest.raises(ValueError, match="workspace_scope_mismatch"):
        await _execute_external_status_check(
            settings=settings,
            client=FakeClient(),
            providers=["github"],
            workspace_id="ws_explicit",
            check_id=None,
            wait_for_result=False,
            response_mode="compact",
            token_workspace_id="ws_from_token",
        )
