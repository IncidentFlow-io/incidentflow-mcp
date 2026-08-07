"""Universal, read-only integration guide tool.

``integration_guide`` turns a ``(integration, goal, method)`` request into a
structured, source-cited, secret-free set of steps for connecting, configuring,
verifying, upgrading, troubleshooting, or removing an IncidentFlow integration.

Design constraints:

* **Never handles secrets.** Steps only ever contain command *templates* with
  ``<placeholder>`` values; the real one-time tokens / API keys are generated in
  the IncidentFlow application and are listed in ``sensitive_inputs`` so a client
  knows what the user must supply — they are never accepted, returned, or logged.
* **Deterministic, curated steps.** The step list, requirements, and verification
  come from per-integration providers (curated Python), not from a language model
  or from raw upstream data, so the response is strict and safe.
* **Documentation-backed.** ``sources[]`` (and a short summary) are populated from
  a filtered search over the public documentation collection.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.knowledge_search_tools import PlatformAPIKnowledgeClient

logger = logging.getLogger(__name__)

Integration = Literal["kubernetes", "slack", "grafana", "argocd"]
Goal = Literal["install", "configure", "verify", "upgrade", "troubleshoot", "uninstall"]
ResponseMode = Literal["compact", "full"]

_GOAL_TO_DOCTYPE: dict[str, str | None] = {
    "install": "installation_guide",
    "configure": "configuration_reference",
    "verify": "verification_guide",
    "upgrade": "upgrade_guide",
    "troubleshoot": "troubleshooting_guide",
    "uninstall": None,
}

# Generic application destination per integration. Kept to stable, non-fabricated
# routes; the specific screen is named in the step instruction text.
_APP_URLS: dict[str, str] = {
    "kubernetes": "https://app.incidentflow.io",
    "slack": "https://app.incidentflow.io/integrations",
    "grafana": "https://app.incidentflow.io/integrations",
    "argocd": "https://app.incidentflow.io/integrations",
}


# ---------------------------------------------------------------------------
# Output models (strict)
# ---------------------------------------------------------------------------
class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    required: bool = True
    satisfied: bool | None = None


class GuideStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    title: str
    instruction: str
    command_template: str | None = None
    app_url: str | None = None
    requires_confirmation: bool = False
    sensitive_inputs: list[str] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["command", "mcp_tool", "http_request", "manual"]
    description: str
    command: str | None = None
    tool_name: str | None = None
    expected: dict[str, Any] | None = None


class GuideSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str | None = None
    title: str
    url: str
    section: str | None = None
    docs_version: str | None = None
    updated_at: str | None = None


class IntegrationGuideOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    goal: str
    method: str | None = None
    summary: str
    requirements: list[Requirement] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    steps: list[GuideStep] = Field(default_factory=list)
    verification: list[VerificationCheck] = Field(default_factory=list)
    sources: list[GuideSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class GuideProvider:
    """Base provider. Subclasses supply curated, secret-free guidance."""

    integration: Integration
    allowed_methods: tuple[str, ...] = ()
    default_method: str | None = None
    verification_tool: str = ""
    display_name: str = ""

    def resolve_method(self, method: str | None) -> tuple[str | None, list[str]]:
        warnings: list[str] = []
        if method is None:
            return self.default_method, warnings
        if self.allowed_methods and method not in self.allowed_methods:
            warnings.append(
                f"Unsupported method '{method}' for {self.integration}; "
                f"supported methods: {', '.join(self.allowed_methods)}."
            )
            return self.default_method, warnings
        return method, warnings

    def requirements(self, goal: str, ctx: Mapping[str, Any]) -> list[Requirement]:
        return []

    def steps(self, goal: str, method: str | None, ctx: Mapping[str, Any]) -> list[GuideStep]:
        raise NotImplementedError

    def verification(self, goal: str) -> list[VerificationCheck]:
        if not self.verification_tool:
            return []
        return [
            VerificationCheck(
                type="mcp_tool",
                description=f"Confirm the {self.display_name} connection is healthy.",
                tool_name=self.verification_tool,
                expected={"healthy": True},
            )
        ]

    def summary(self, goal: str, method: str | None) -> str:
        method_note = f" using {method}" if method else ""
        return f"How to {goal} the {self.display_name} integration{method_note}."

    # Shared helpers -------------------------------------------------------
    def _app_step(self, number: int, title: str, instruction: str) -> GuideStep:
        return GuideStep(
            number=number,
            title=title,
            instruction=instruction,
            app_url=_APP_URLS[self.integration],
            requires_confirmation=False,
        )

    def _uninstall_note(self) -> str:
        return (
            f"Removing the {self.display_name} integration stops IncidentFlow from reading "
            "its signals. Existing incident history is retained."
        )


def _satisfied(ctx: Mapping[str, Any], key: str) -> bool:
    value = ctx.get(key)
    return value is not None and str(value).strip() != ""


class KubernetesGuideProvider(GuideProvider):
    integration = "kubernetes"
    allowed_methods = ("helm", "argocd", "terraform", "flux", "raw_yaml")
    default_method = "helm"
    verification_tool = "k8s_agent_status"
    display_name = "Kubernetes"

    def requirements(self, goal: str, ctx: Mapping[str, Any]) -> list[Requirement]:
        reqs = [
            Requirement(
                name="workspace_access",
                description="Permission to create a Kubernetes Agent in IncidentFlow.",
                required=True,
                satisfied=None,
            )
        ]
        if goal in {"install", "configure", "upgrade"}:
            reqs.append(
                Requirement(
                    name="cluster_name",
                    description="Stable cluster identifier used to register the agent.",
                    required=True,
                    satisfied=_satisfied(ctx, "cluster_name"),
                )
            )
            reqs.append(
                Requirement(
                    name="kubectl",
                    description="kubectl configured for the target cluster.",
                    required=True,
                    satisfied=None,
                )
            )
        return reqs

    def steps(self, goal: str, method: str | None, ctx: Mapping[str, Any]) -> list[GuideStep]:
        ns = str(ctx.get("namespace") or "incidentflow-agent")
        cluster = "<cluster-identifier>"
        if goal == "install":
            install_cmd = {
                "helm": (
                    "helm repo add incidentflow https://charts.incidentflow.io && "
                    "helm repo update && "
                    "helm upgrade --install incidentflow-k8s-agent incidentflow/k8s-agent "
                    f"--namespace {ns} --create-namespace "
                    f"--set clusterName={cluster} "
                    "--set registrationToken=<one-time-registration-token>"
                ),
                "raw_yaml": (
                    "kubectl apply -f <incidentflow-agent-manifest-generated-in-app.yaml>"
                ),
            }.get(method or "helm")
            return [
                self._app_step(
                    1,
                    "Register the agent",
                    "Open IncidentFlow → Kubernetes Agents, select Install Agent, and enter "
                    "a cluster display name and unique cluster identifier.",
                ),
                self._app_step(
                    2,
                    "Copy the one-time registration token",
                    "The token is shown once, expires in 24 hours, and is consumed on first "
                    "registration. Always use the command generated for your workspace.",
                ),
                GuideStep(
                    number=3,
                    title="Run the generated install command",
                    instruction=(
                        f"Install the read-only agent with {method or 'helm'}. The real token "
                        "is supplied by the installation screen — the template below only shows "
                        "its shape."
                    ),
                    command_template=install_cmd,
                    requires_confirmation=True,
                    sensitive_inputs=["registration_token"],
                ),
            ]
        if goal == "configure":
            return [
                self._app_step(
                    1,
                    "Review namespace access",
                    "Limit the agent to explicitly approved namespaces unless cluster-wide "
                    "read-only visibility is required.",
                ),
                GuideStep(
                    number=2,
                    title="Apply configuration",
                    instruction="Update Helm values or GitOps config and redeploy the agent.",
                    command_template=(
                        "helm upgrade incidentflow-k8s-agent incidentflow/k8s-agent "
                        f"--namespace {ns} --reuse-values --set logLevel=<log-level>"
                    ),
                    requires_confirmation=True,
                ),
            ]
        if goal == "upgrade":
            return [
                GuideStep(
                    number=1,
                    title="Upgrade the agent",
                    instruction=(
                        "A normal upgrade needs no new registration token. Pin a specific "
                        "chart version in production."
                    ),
                    command_template=(
                        "helm repo update && "
                        "helm upgrade incidentflow-k8s-agent incidentflow/k8s-agent "
                        f"--namespace {ns} --version <target-chart-version> --reuse-values"
                    ),
                    requires_confirmation=True,
                ),
            ]
        if goal == "troubleshoot":
            return [
                GuideStep(
                    number=1,
                    title="Inspect the agent workload",
                    instruction="Check pod status and recent logs for registration or "
                    "connection errors. Never print the registration token.",
                    command_template=(
                        f"kubectl get pods -n {ns} && "
                        f"kubectl logs -n {ns} deployment/incidentflow-k8s-agent --tail=100"
                    ),
                ),
                GuideStep(
                    number=2,
                    title="Verify read-only permissions",
                    instruction="Confirm the agent's effective access without escalating to "
                    "cluster-admin.",
                    command_template=(
                        "kubectl auth can-i list pods "
                        f"--as system:serviceaccount:{ns}:incidentflow-k8s-agent "
                        "--all-namespaces"
                    ),
                ),
            ]
        if goal == "uninstall":
            return [
                GuideStep(
                    number=1,
                    title="Remove the agent",
                    instruction=self._uninstall_note(),
                    command_template=f"helm uninstall incidentflow-k8s-agent --namespace {ns}",
                    requires_confirmation=True,
                ),
            ]
        # verify
        return [
            GuideStep(
                number=1,
                title="Confirm the agent is connected",
                instruction="Check the agent workload rollout and then confirm connectivity "
                "from an MCP client.",
                command_template=(
                    f"kubectl rollout status deployment/incidentflow-k8s-agent --namespace {ns}"
                ),
            ),
        ]


class _AppConnectProvider(GuideProvider):
    """Shared shape for app-connected integrations (Slack/Grafana/Argo CD)."""

    connect_label = "Connect"
    credential_note = ""

    def requirements(self, goal: str, ctx: Mapping[str, Any]) -> list[Requirement]:
        return [
            Requirement(
                name="workspace_admin",
                description=f"Permission to connect the {self.display_name} integration.",
                required=True,
                satisfied=None,
            )
        ]

    def steps(self, goal: str, method: str | None, ctx: Mapping[str, Any]) -> list[GuideStep]:
        if goal in {"install", "configure"}:
            return [
                self._app_step(
                    1,
                    f"{self.connect_label} {self.display_name}",
                    f"Open IncidentFlow → Integrations and select {self.connect_label} "
                    f"{self.display_name}"
                    + (f" ({method})." if method else ".")
                    + (f" {self.credential_note}" if self.credential_note else ""),
                ),
                self._app_step(
                    2,
                    "Approve access scope",
                    f"Grant the minimum read-only scope IncidentFlow requests for "
                    f"{self.display_name}.",
                ),
            ]
        if goal == "troubleshoot":
            return [
                self._app_step(
                    1,
                    "Re-check the connection",
                    f"Open IncidentFlow → Integrations and re-run the {self.display_name} "
                    "connection test. Confirm the credential has not expired or been revoked.",
                ),
            ]
        if goal == "uninstall":
            return [
                self._app_step(
                    1,
                    f"Disconnect {self.display_name}",
                    self._uninstall_note() + " Disconnect it from IncidentFlow → Integrations.",
                ),
            ]
        # verify / upgrade
        return [
            self._app_step(
                1,
                f"Confirm {self.display_name} is connected",
                f"Open IncidentFlow → Integrations and confirm {self.display_name} shows as "
                "connected, then verify from an MCP client.",
            ),
        ]


class SlackGuideProvider(_AppConnectProvider):
    integration = "slack"
    allowed_methods = ("oauth", "manifest")
    default_method = "oauth"
    verification_tool = "incidentflow_integrations_status"
    display_name = "Slack"
    connect_label = "Connect"
    credential_note = "The OAuth flow is completed in the application; no tokens are pasted."


class GrafanaGuideProvider(_AppConnectProvider):
    integration = "grafana"
    allowed_methods = ("service_account", "api_token")
    default_method = "service_account"
    verification_tool = "grafana_connection_health"
    display_name = "Grafana"
    connect_label = "Connect"
    credential_note = (
        "Use a Grafana service account token with read-only access; it is stored server-side."
    )


class ArgoCDGuideProvider(_AppConnectProvider):
    integration = "argocd"
    allowed_methods = ("api_token", "oauth")
    default_method = "api_token"
    verification_tool = "argocd_connection_health"
    display_name = "Argo CD"
    connect_label = "Connect"
    credential_note = "Use a read-only Argo CD API token; it is stored server-side."


GUIDE_PROVIDERS: dict[str, GuideProvider] = {
    "kubernetes": KubernetesGuideProvider(),
    "slack": SlackGuideProvider(),
    "grafana": GrafanaGuideProvider(),
    "argocd": ArgoCDGuideProvider(),
}


# ---------------------------------------------------------------------------
# Docs-backed sources
# ---------------------------------------------------------------------------
async def _fetch_sources(
    settings: Settings,
    *,
    integration: str,
    goal: str,
    method: str | None,
    problem: str | None,
    version: str | None,
    limit: int,
) -> tuple[list[GuideSource], list[str]]:
    warnings: list[str] = []
    query = " ".join(
        part for part in [integration, goal, method or "", problem or ""] if part
    ).strip()
    try:
        client = PlatformAPIKnowledgeClient(settings)
        payload = await client.search_docs(
            query=query,
            limit=limit,
            document_type=_GOAL_TO_DOCTYPE.get(goal),
            integration=integration,
            installation_method=method if integration == "kubernetes" else None,
            product_version=version,
        )
    except Exception as exc:
        # Documentation search is best-effort; curated steps must still be returned.
        logger.warning("integration_guide docs search failed: %s", exc)
        warnings.append("Documentation search is unavailable; showing curated steps only.")
        return [], warnings

    sources: list[GuideSource] = []
    seen: set[str] = set()
    for match in payload.get("matches", []) or []:
        url = str(match.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            GuideSource(
                title=str(match.get("title") or url),
                url=url,
                section=match.get("section") or None,
            )
        )
    return sources, warnings


async def integration_guide(
    settings: Settings,
    *,
    integration: str,
    goal: str,
    method: str | None = None,
    environment: str | None = None,
    version: str | None = None,
    problem: str | None = None,
    context: Mapping[str, Any] | None = None,
    response_mode: str = "compact",
) -> IntegrationGuideOutput:
    provider = GUIDE_PROVIDERS[integration]
    ctx: Mapping[str, Any] = context or {}

    resolved_method, warnings = provider.resolve_method(method)
    requirements = provider.requirements(goal, ctx)
    steps = provider.steps(goal, resolved_method, ctx)
    verification = provider.verification(goal)
    missing_inputs = [req.name for req in requirements if req.required and req.satisfied is False]

    limit = 6 if response_mode == "full" else 3
    sources, source_warnings = await _fetch_sources(
        settings,
        integration=integration,
        goal=goal,
        method=resolved_method,
        problem=problem,
        version=version,
        limit=limit,
    )
    warnings.extend(source_warnings)

    if environment:
        warnings.append(f"Guidance targets the '{environment}' environment where relevant.")

    return IntegrationGuideOutput(
        integration=integration,
        goal=goal,
        method=resolved_method,
        summary=provider.summary(goal, resolved_method),
        requirements=requirements,
        missing_inputs=missing_inputs,
        steps=steps,
        verification=verification,
        sources=sources,
        warnings=warnings,
    )
