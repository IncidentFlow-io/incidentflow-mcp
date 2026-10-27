"""Unit tests for the integration_guide tool and grafana_connection_health mapping."""

from __future__ import annotations

import re

import pytest

from incidentflow_mcp.tools.grafana import grafana_connection_health
from incidentflow_mcp.tools.integration_guide import (
    GUIDE_PROVIDERS,
    IntegrationGuideOutput,
    route_integration_question,
)

GOALS = ["install", "configure", "verify", "upgrade", "troubleshoot", "uninstall"]

_SECRET_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}|\b[A-Za-z0-9]{32,}\b")


@pytest.mark.parametrize("integration", list(GUIDE_PROVIDERS))
@pytest.mark.parametrize("goal", GOALS)
def test_every_provider_goal_produces_secret_free_steps(integration: str, goal: str) -> None:
    provider = GUIDE_PROVIDERS[integration]
    method, _ = provider.resolve_method(None)
    steps = provider.steps(goal, method, {})
    assert steps, f"{integration}/{goal} produced no steps"
    for step in steps:
        template = step.command_template or ""
        # Command templates may only contain placeholders, never real secrets.
        assert not _SECRET_RE.search(template), f"secret-like token in {integration}/{goal}"
        if "registration-token" in template or "registrationToken=" in template:
            assert "registration_token" in step.sensitive_inputs


def test_unsupported_method_falls_back_with_warning() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    method, warnings = provider.resolve_method("brew")
    assert method == "helm"
    assert any("Unsupported method" in w for w in warnings)


def test_kubernetes_missing_cluster_name_is_reported() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    missing = [r for r in provider.requirements("install", {}) if r.name == "cluster_name"]
    assert missing and missing[0].satisfied is False
    satisfied = [
        r
        for r in provider.requirements("install", {"cluster_name": "prod"})
        if r.name == "cluster_name"
    ]
    assert satisfied and satisfied[0].satisfied is True


def test_kubernetes_install_requirements_cover_safe_helm_prerequisites() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    requirement_names = {item.name for item in provider.requirements("install", {})}
    assert {
        "cluster_name",
        "kubectl",
        "helm_3",
        "outbound_https_wss",
        "namespace_and_read_only_rbac",
    } <= requirement_names


def test_kubernetes_helm_install_uses_bootstrap_secret_not_token_value() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    step = provider.steps("install", "helm", {"cluster_name": "prod-eu-1"})[-1]
    command = step.command_template or ""
    assert "registrationTokenSecretName=incidentflow-k8s-agent-bootstrap" in command
    assert "registrationToken=" not in command
    assert "agent.platformUrl" not in command
    assert "agent.gatewayUrl" not in command
    assert step.command is not None
    assert step.command.executable == "helm"
    assert step.command.contains_secret is False


def test_kubernetes_hosted_install_contract_has_no_internal_or_secret_values() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    step = provider.steps("install", "helm", {"cluster_name": "prod-eu-1"})[-1]
    payload = step.model_dump_json()
    for forbidden in (
        "platformUrl",
        "gatewayUrl",
        "localhost",
        "host.docker.internal",
        "registration-token",
        "registrationToken=",
    ):
        assert forbidden not in payload


def test_kubernetes_install_without_cluster_name_blocks_helm_command() -> None:
    step = GUIDE_PROVIDERS["kubernetes"].steps("install", "helm", {})[-1]
    assert step.state == "blocked"
    assert step.command is None


def test_guide_me_routing_sends_kubernetes_install_language_to_curated_guide() -> None:
    assert route_integration_question("Помоги подключить Kubernetes cluster через Helm") == (
        "kubernetes",
        "install",
    )


def test_kubernetes_verify_includes_read_only_rbac_and_remediation() -> None:
    checks = GUIDE_PROVIDERS["kubernetes"].verification("verify")
    rbac = next(check for check in checks if check.tool_name == "k8s_rbac_check")
    assert rbac.expected == {"read_only": True}
    assert "If permissions are missing" in rbac.description


def test_verification_tools_are_correct() -> None:
    assert GUIDE_PROVIDERS["kubernetes"].verification_tool == "k8s_agent_status"
    assert GUIDE_PROVIDERS["slack"].verification_tool == "incidentflow_integrations_status"
    assert GUIDE_PROVIDERS["grafana"].verification_tool == "grafana_connection_health"
    assert GUIDE_PROVIDERS["argocd"].verification_tool == "argocd_connection_health"


def test_output_model_is_strict() -> None:
    assert IntegrationGuideOutput.model_config.get("extra") == "forbid"


class _FakeGrafanaClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def health(self) -> dict:
        return self._payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_healthy"),
    [
        ({"ok": True, "message": "connection_ok", "datasources_found": 2}, "connected", True),
        ({"ok": False, "message": "grafana_not_configured"}, "not_configured", False),
        ({"ok": False, "message": "grafana_no_datasources"}, "degraded", False),
        ({"ok": False, "message": "datasource_query_test_failed"}, "degraded", False),
        ({"ok": False, "message": "something_odd"}, "unknown", False),
    ],
)
async def test_grafana_connection_health_status_mapping(
    payload: dict, expected_status: str, expected_healthy: bool
) -> None:
    result = await grafana_connection_health(_FakeGrafanaClient(payload))
    assert result.status == expected_status
    assert result.healthy is expected_healthy
    assert result.ok is bool(payload.get("ok"))
