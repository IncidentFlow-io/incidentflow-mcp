"""Unit tests for the integration_guide tool and grafana_connection_health mapping."""

from __future__ import annotations

import re
from typing import Any

import pytest

import incidentflow_mcp.tools.integration_guide as integration_guide_module
from incidentflow_mcp.tools.grafana import grafana_connection_health
from incidentflow_mcp.tools.integration_guide import (
    GUIDE_PROVIDERS,
    GuideSource,
    GuideStep,
    IntegrationGuideOutput,
    UnsafeIntegrationGuideTemplateError,
    _reject_unsafe_guide,
    guide_me,
    integration_guide,
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


def test_kubernetes_missing_cluster_identifier_is_reported() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    missing = [r for r in provider.requirements("install", {}) if r.name == "cluster_identifier"]
    assert missing and missing[0].satisfied is False
    satisfied = [
        r
        for r in provider.requirements("install", {"cluster_identifier": "prod"})
        if r.name == "cluster_identifier"
    ]
    assert satisfied and satisfied[0].satisfied is True


def test_install_identifier_prompt_is_the_only_initial_user_question() -> None:
    # The provider blocks Helm until the identifier is supplied; it does not
    # inspect an existing connected cluster to invent a target.
    steps = GUIDE_PROVIDERS["kubernetes"].steps("install", "helm", {})
    install = next(step for step in steps if step.id == "install_helm_chart")
    assert install.state == "blocked"
    assert install.command is None


async def _no_docs(*args: Any, **kwargs: Any) -> tuple[list[GuideSource], list[str]]:
    return [], []


@pytest.mark.asyncio
async def test_new_kubernetes_install_returns_only_the_identifier_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_guide_module, "_fetch_sources", _no_docs)
    result = await integration_guide(
        object(),  # type: ignore[arg-type]
        integration="kubernetes",
        goal="install",
        method="helm",
    )

    assert result.status == "needs_input"
    assert result.steps == []
    assert result.summary == (
        "Я помогу подключить новый Kubernetes cluster через Helm с read-only RBAC.\n"
        "Какой `cluster identifier` использовать? Например: `production-eu1`."
    )


@pytest.mark.asyncio
async def test_kubernetes_install_with_identifier_returns_the_expected_next_dialogue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_guide_module, "_fetch_sources", _no_docs)
    result = await integration_guide(
        object(),  # type: ignore[arg-type]
        integration="kubernetes",
        goal="install",
        method="helm",
        context={"cluster_identifier": "production-eu1"},
    )

    assert result.status == "success"
    assert [step.id for step in result.steps] == [
        "register_agent",
        "create_bootstrap_secret",
        "install_helm_chart",
        "verify_connection",
    ]
    assert result.verification == []
    assert "именно `production-eu1` через `k8s_agent_status`" in result.summary


@pytest.mark.asyncio
async def test_kubernetes_verify_requires_explicit_helm_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_guide_module, "_fetch_sources", _no_docs)
    base = {"integration": "kubernetes", "goal": "verify", "context": {"cluster_identifier": "production-eu1"}}
    blocked = await integration_guide(object(), **base)  # type: ignore[arg-type]
    ready = await integration_guide(
        object(),  # type: ignore[arg-type]
        integration="kubernetes",
        goal="verify",
        context={"cluster_identifier": "production-eu1", "helm_install_completed": True},
    )

    assert blocked.status == "needs_input"
    assert blocked.missing_inputs == ["helm_install_completed"]
    assert ready.status == "success"
    assert any(check.tool_name == "k8s_agent_status" for check in ready.verification)


@pytest.mark.asyncio
async def test_guide_me_routes_user_completion_to_kubernetes_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_guide_module, "_fetch_sources", _no_docs)
    result = await guide_me(
        object(),  # type: ignore[arg-type]
        question="готово",
        context={"cluster_identifier": "production-eu1", "helm_install_completed": True},
    )

    assert result.goal == "verify"
    assert result.integration == "kubernetes"
    assert any(check.tool_name == "k8s_agent_status" for check in result.verification)


@pytest.mark.asyncio
async def test_kubernetes_identifier_rejects_helm_value_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_guide_module, "_fetch_sources", _no_docs)
    with pytest.raises(ValueError, match="DNS label"):
        await integration_guide(
            object(),  # type: ignore[arg-type]
            integration="kubernetes",
            goal="install",
            method="helm",
            context={"cluster_identifier": "production-eu1,registrationToken=unsafe"},
        )


def test_kubernetes_install_requirements_cover_safe_helm_prerequisites() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    requirement_names = {item.name for item in provider.requirements("install", {})}
    assert {
        "cluster_identifier",
        "kubectl",
        "helm_3",
        "outbound_https_wss",
        "namespace_and_read_only_rbac",
        "supported_kubernetes_version",
    } <= requirement_names


def test_kubernetes_helm_install_uses_bootstrap_secret_not_token_value() -> None:
    provider = GUIDE_PROVIDERS["kubernetes"]
    step = next(step for step in provider.steps("install", "helm", {"cluster_identifier": "prod-eu-1"}) if step.id == "install_helm_chart")
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
    step = next(step for step in provider.steps("install", "helm", {"cluster_identifier": "prod-eu-1"}) if step.id == "install_helm_chart")
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


def test_kubernetes_install_without_cluster_identifier_blocks_helm_command() -> None:
    step = next(step for step in GUIDE_PROVIDERS["kubernetes"].steps("install", "helm", {}) if step.id == "install_helm_chart")
    assert step.state == "blocked"
    assert step.command is None


def test_kubernetes_install_uses_required_safe_step_sequence() -> None:
    steps = GUIDE_PROVIDERS["kubernetes"].steps("install", "helm", {"cluster_identifier": "prod-eu-1"})
    assert [step.id for step in steps] == [
        "register_agent",
        "create_bootstrap_secret",
        "install_helm_chart",
        "verify_connection",
    ]
    assert steps[0].inputs == ["cluster_identifier"]
    assert steps[1].sensitive_inputs == ["registration_token"]
    assert steps[1].token_handling == "ui_only"
    assert steps[2].command_contains_secret is False


@pytest.mark.parametrize(
    "unsafe_template",
    [
        "helm upgrade --set registrationToken=do-not-render",
        "helm upgrade --set=registrationToken=do-not-render",
        "helm upgrade --set-string registrationToken=do-not-render",
        "helm upgrade --set-string clusterName=production-eu1,registrationToken=do-not-render",
    ],
)
def test_stale_raw_token_template_is_suppressed(unsafe_template: str) -> None:
    unsafe = IntegrationGuideOutput(
        integration="kubernetes",
        goal="install",
        summary="test",
        steps=[
            GuideStep(
                number=1,
                title="unsafe",
                instruction="test",
                command_template=unsafe_template,
            )
        ],
        sources=[GuideSource(document_id="test", title="test", url="https://docs.example.test", section="test", docs_version="v1", updated_at="2026-10-27")],
        security_notes=["test"],
    )
    with pytest.raises(UnsafeIntegrationGuideTemplateError, match="outdated"):
        _reject_unsafe_guide(unsafe)


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
