from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import (
    _compact_external_status_result,
    _compact_log_payload,
    _execute_external_status_check,
    _filter_workload_pods,
    _k8s_cluster_overview_payload,
    _k8s_connection_health_payload,
    _k8s_rbac_check_payload,
    _normalize_correlation_alerts,
    _normalize_polled_external_status_job,
    _overview_payload,
    _resolve_correlation_mode,
    _resolve_execution_mode,
    _resolve_job_workspace_id,
    _resolve_k8s_cluster_id,
    _resolve_slack_tool_access,
    _select_workload_pod,
)
from incidentflow_mcp.platform_api.ai_jobs_client import PlatformAPIJobsClient
from incidentflow_mcp.tools.registry import get_tool_specs


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


def test_normalize_polled_external_status_job_running_returns_async() -> None:
    output = _normalize_polled_external_status_job(
        job_id="job_1",
        job={"status": "running"},
        poll_after_seconds=2,
        response_mode="compact",
    )
    payload = json.loads(output)

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
                                "incident_updates": [{"body": "very large payload"}],
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
    payload = json.loads(output)

    assert payload["status"] == "ok"
    assert payload["checked_at"] == "2026-03-17T18:00:00Z"
    provider = payload["providers"][0]
    compact_incident = provider["active_incidents"][0]
    assert compact_incident["id"] == "inc_1"
    assert "incident_updates" not in compact_incident
    assert provider["regional_status_errors"] == {"eu": "404 Not Found"}


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
    payload = json.loads(output)

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
    payload = json.loads(output)

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
    payload = json.loads(output)

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
    payload = json.loads(output)

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
    payload = json.loads(output)

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
