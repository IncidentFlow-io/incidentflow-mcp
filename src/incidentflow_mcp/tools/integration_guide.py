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
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.knowledge_search_tools import PlatformAPIKnowledgeClient

logger = logging.getLogger(__name__)

Integration = Literal["kubernetes", "slack", "grafana", "argocd"]
Goal = Literal["install", "configure", "verify", "upgrade", "troubleshoot", "uninstall"]
ResponseMode = Literal["compact", "full"]
GuideStatus = Literal["success", "needs_input"]
StepState = Literal["blocked", "current", "completed", "pending"]

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

_HOSTED_K8S_CHART_VERSION = "1.0.14"


# ---------------------------------------------------------------------------
# Output models (strict)
# ---------------------------------------------------------------------------
class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    required: bool = True
    satisfied: bool | None = None


class GuideCommand(BaseModel):
    """A safe, copyable command without relying on shell parsing by clients."""

    model_config = ConfigDict(extra="forbid")

    executable: str
    args: list[str]
    rendered_shell: str
    copyable: bool = True
    contains_secret: bool = False


class GuideStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    number: int = Field(ge=1)
    title: str
    instruction: str
    state: StepState = "pending"
    user_action_required: bool = False
    completion_signal: str | None = None
    command: GuideCommand | None = None
    # Deprecated compatibility field. New clients should use command.rendered_shell.
    command_template: str | None = None
    app_url: str | None = None
    requires_confirmation: bool = False
    sensitive_inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_stable_id(self) -> "GuideStep":
        if not self.id:
            self.id = re.sub(r"[^a-z0-9]+", "_", self.title.lower()).strip("_")
        return self


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

    status: GuideStatus = "success"
    integration: str
    goal: str
    method: str | None = None
    summary: str
    requirements: list[Requirement] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    next_action: dict[str, str] | None = None
    steps: list[GuideStep] = Field(default_factory=list)
    verification: list[VerificationCheck] = Field(default_factory=list)
    sources: list[GuideSource] = Field(min_length=1)
    security_notes: list[str] = Field(min_length=1)
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
    def _app_step(
        self,
        number: int,
        title: str,
        instruction: str,
        *,
        step_id: str | None = None,
        state: StepState = "pending",
        completion_signal: str | None = None,
    ) -> GuideStep:
        return GuideStep(
            id=step_id or "",
            number=number,
            title=title,
            instruction=instruction,
            state=state,
            user_action_required=True,
            completion_signal=completion_signal,
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


def _shell(executable: str, args: list[str]) -> str:
    """Render trusted, curated argv as a readable multi-line shell command."""
    return " ".join([executable, *args])


def _command(executable: str, args: list[str], *, rendered_shell: str | None = None) -> GuideCommand:
    return GuideCommand(
        executable=executable,
        args=args,
        rendered_shell=rendered_shell or _shell(executable, args),
    )


class KubernetesGuideProvider(GuideProvider):
    integration = "kubernetes"
    allowed_methods = ("helm",)
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
            reqs.extend(
                [
                    Requirement(
                        name="helm_3",
                        description=(
                            "Helm 3 is installed and can install releases in the target cluster."
                        ),
                        required=True,
                        satisfied=None,
                    ),
                    Requirement(
                        name="outbound_https_wss",
                        description=(
                            "The cluster can make outbound HTTPS and secure WebSocket (WSS) "
                            "connections to IncidentFlow; no inbound cluster access is required."
                        ),
                        required=True,
                        satisfied=None,
                    ),
                    Requirement(
                        name="namespace_and_read_only_rbac",
                        description=(
                            "Permission to create the agent namespace, ServiceAccount, bootstrap "
                            "Secret, and read-only RBAC resources. The Agent receives no write "
                            "permissions to workload resources."
                        ),
                        required=True,
                        satisfied=None,
                    ),
                ]
            )
        return reqs

    def steps(self, goal: str, method: str | None, ctx: Mapping[str, Any]) -> list[GuideStep]:
        ns = str(ctx.get("namespace") or "incidentflow-agent")
        cluster = str(ctx.get("cluster_name") or "")
        if goal == "install":
            install_command = None
            if cluster:
                install_args = [
                    "upgrade", "--install", "incidentflow-k8s-agent",
                    "oci://ghcr.io/incidentflow-io/charts/incidentflow-k8s-agent",
                    "--version", _HOSTED_K8S_CHART_VERSION,
                    "--namespace", ns, "--create-namespace",
                    "--set-string", f"clusterName={cluster}",
                    "--set", "registrationTokenSecretName=incidentflow-k8s-agent-bootstrap",
                ]
                install_command = _command(
                    "helm",
                    install_args,
                    rendered_shell=(
                        "helm upgrade --install incidentflow-k8s-agent "
                        "oci://ghcr.io/incidentflow-io/charts/incidentflow-k8s-agent \\\n"
                        f"  --version {_HOSTED_K8S_CHART_VERSION} \\\n"
                        f"  --namespace {ns} --create-namespace \\\n"
                        f"  --set-string clusterName={cluster} \\\n"
                        "  --set registrationTokenSecretName=incidentflow-k8s-agent-bootstrap"
                    ),
                )
            return [
                self._app_step(
                    1,
                    "Register the agent",
                    "Open IncidentFlow → Kubernetes Agents → Install Agent and enter "
                    "the cluster name. The Agent receives read-only Kubernetes permissions.",
                    step_id="register_agent",
                    state="current",
                    completion_signal="agent_registered",
                ),
                self._app_step(
                    2,
                    "Create the bootstrap Secret",
                    "Create the one-time registration token, then run the protected “Run first” "
                    "command generated by IncidentFlow. It creates the namespace and bootstrap "
                    "Secret. Never paste the token into chat, Git, docs, logs, or knowledge.",
                    step_id="bootstrap_secret",
                    state="blocked" if not cluster else "pending",
                    completion_signal="bootstrap_secret_created",
                ),
                GuideStep(
                    id="helm_install",
                    number=3,
                    title="Install with Helm",
                    instruction=(
                        "Run Helm after the bootstrap Secret exists. Helm receives the Secret "
                        "name, not a raw registration token. For hosted IncidentFlow, the chart "
                        "uses the production platform and gateway URLs by default; override them "
                        "only for a self-hosted deployment."
                    ),
                    state="blocked" if not cluster else "pending",
                    user_action_required=True,
                    completion_signal="helm_release_installed",
                    command=install_command,
                    command_template=(install_command.rendered_shell if install_command else None),
                    requires_confirmation=True,
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
                        "helm upgrade incidentflow-k8s-agent "
                        "oci://ghcr.io/incidentflow-io/charts/incidentflow-k8s-agent "
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
                        "helm upgrade incidentflow-k8s-agent "
                        "oci://ghcr.io/incidentflow-io/charts/incidentflow-k8s-agent "
                        f'--namespace {ns} --version "<target-chart-version>" --reuse-values'
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
                title="Confirm the Helm rollout",
                instruction="Confirm that the agent deployment is available before checking its "
                "IncidentFlow connection.",
                command_template=(
                    f"kubectl rollout status deployment/incidentflow-k8s-agent --namespace {ns}"
                ),
            ),
            GuideStep(
                number=2,
                title="Check connection from MCP",
                instruction=(
                    "Use k8s_agent_status and expect healthy=true. IncidentFlow then confirms "
                    "token acceptance, credentials, gateway connection, heartbeat, and "
                    "read-only RBAC. "
                    "If it is unhealthy, inspect the rollout and outbound connectivity, then use "
                    "the Kubernetes troubleshooting guide; do not reveal Secret values or tokens."
                ),
            ),
        ]

    def verification(self, goal: str) -> list[VerificationCheck]:
        if goal != "verify":
            return super().verification(goal)
        return [
            VerificationCheck(
                type="command",
                description=(
                    "The IncidentFlow Agent deployment has completed its rollout. If it fails, "
                    "inspect the Helm release, Pod events, and sanitized agent logs before "
                    "retrying."
                ),
                command=(
                    "kubectl rollout status deployment/incidentflow-k8s-agent "
                    "--namespace incidentflow-agent"
                ),
            ),
            VerificationCheck(
                type="mcp_tool",
                description=(
                    "The Agent is connected and has a recent heartbeat. If it is unhealthy, "
                    "check outbound HTTPS/WSS connectivity and the IncidentFlow registration state."
                ),
                tool_name="k8s_agent_status",
                expected={"healthy": True},
            ),
            VerificationCheck(
                type="mcp_tool",
                description=(
                    "The Agent's effective Kubernetes access remains read-only. If permissions are "
                    "missing or write access is reported, correct the chart-managed RBAC to the "
                    "minimum read-only scope and rerun this check."
                ),
                tool_name="k8s_rbac_check",
                expected={"read_only": True},
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
            component="k8s-agent" if integration == "kubernetes" else integration,
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

    # A guide must remain cited even when the search service is temporarily unavailable.
    # The deterministic fallback is intentionally public and non-secret.
    if not sources:
        sources = [
            GuideSource(
                document_id=(
                    "k8s-agent-installation" if integration == "kubernetes" and goal == "install" else None
                ),
                title=(
                    "Install Kubernetes Agent with Helm"
                    if integration == "kubernetes" and goal == "install"
                    else f"IncidentFlow {integration.title()} integration guide"
                ),
                url=(
                    "https://docs.incidentflow.io/docs/integrations/kubernetes/installation"
                    if integration == "kubernetes"
                    else "https://docs.incidentflow.io/docs/integrations"
                ),
                section="Install with Helm" if integration == "kubernetes" and goal == "install" else None,
            )
        ]

    if environment:
        warnings.append(f"Guidance targets the '{environment}' environment where relevant.")

    next_action = None
    if missing_inputs:
        field = missing_inputs[0]
        prompt = (
            "How should this cluster appear in IncidentFlow? Example: prod-eu-1"
            if field == "cluster_name"
            else f"Provide the required value for {field}."
        )
        next_action = {"type": "collect_input", "field": field, "prompt": prompt}

    security_notes = [
        "Never place credentials or one-time registration tokens in chat, Git, logs, tickets, documentation, or Qdrant.",
        "Integration guides contain only safe command templates and references to server-side credential storage.",
    ]
    if integration == "kubernetes":
        security_notes.extend(
            [
                "The Kubernetes registration token is one-time and expires after 24 hours.",
                "Helm receives only registrationTokenSecretName; the raw registration token never enters Helm values or release history.",
                "The Agent is granted read-only Kubernetes permissions and no workload mutation, exec, port-forward, or Secret-read access.",
            ]
        )

    return IntegrationGuideOutput(
        status="needs_input" if missing_inputs else "success",
        integration=integration,
        goal=goal,
        method=resolved_method,
        summary=provider.summary(goal, resolved_method),
        requirements=requirements,
        missing_inputs=missing_inputs,
        next_action=next_action,
        steps=steps,
        verification=verification,
        sources=sources,
        security_notes=security_notes,
        warnings=warnings,
    )


def route_integration_question(question: str) -> tuple[Integration, Goal]:
    """Deterministically route setup language to curated guidance, never Qdrant."""
    normalized = question.lower()
    integration: Integration
    if any(term in normalized for term in ("kubernetes", "k8s", "helm", "cluster")):
        integration = "kubernetes"
    elif "grafana" in normalized:
        integration = "grafana"
    elif "argo" in normalized:
        integration = "argocd"
    elif "slack" in normalized:
        integration = "slack"
    else:
        raise ValueError("Specify an integration: kubernetes, slack, grafana, or argocd.")

    if any(term in normalized for term in ("install", "connect", "setup", "set up", "подключ", "установ")):
        return integration, "install"
    if any(term in normalized for term in ("verify", "check", "проверк")):
        return integration, "verify"
    if any(term in normalized for term in ("upgrade", "обнов")):
        return integration, "upgrade"
    if any(term in normalized for term in ("troubleshoot", "problem", "ошиб", "не работает")):
        return integration, "troubleshoot"
    return integration, "configure"


async def guide_me(
    settings: Settings,
    *,
    question: str,
    context: Mapping[str, Any] | None = None,
    response_mode: str = "compact",
) -> IntegrationGuideOutput:
    integration, goal = route_integration_question(question)
    return await integration_guide(
        settings,
        integration=integration,
        goal=goal,
        method="helm" if integration == "kubernetes" and goal == "install" else None,
        context=context,
        response_mode=response_mode,
    )
