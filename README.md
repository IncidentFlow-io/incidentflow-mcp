# incidentflow-mcp

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

The IncidentFlow MCP server is open-source under the MIT License.

IncidentFlow Cloud platform and hosted services are proprietary.

## Local API Docs (OpenAPI + Fern)

This repository includes a code-derived OpenAPI spec and Fern docs config so contributors can inspect the full public API surface locally.

### What is documented

- Public ops endpoints: `/install.sh`, `/healthz`, `/readyz`, `/metrics`
- MCP transport endpoint: `/mcp` (`GET`, `POST`, `OPTIONS`)
- Auth requirements
- Request/response schemas
- Reusable components
- Common error responses (`401`, `403`, `429`, `500`)
- JSON-RPC request examples for MCP (`initialize`, `tools/list`, `tools/call`)

### Prerequisites

Install Fern CLI:

```bash
npm install -g fern-api
```

### Generate and validate OpenAPI

```bash
make openapi-generate
make openapi-validate
```

Output spec:

- `openapi/openapi.yaml`

### Run Fern checks and docs locally

```bash
make fern-check
make fern-docs-dev
```

Alternative direct commands:

```bash
cd fern
FERN_NO_VERSION_REDIRECTION=true fern check
FERN_NO_VERSION_REDIRECTION=true fern docs dev
FERN_NO_VERSION_REDIRECTION=true fern generate --docs --preview
```

`fern generate --docs --preview` may require `fern login` (or `FERN_TOKEN`) depending on your Fern account/workspace setup.

### Custom domain for docs

Fern docs are configured with a custom domain:

- `docs.incidentflow.io`

For production publishing:

```bash
make fern-docs-publish
```

For preview publishing:

```bash
make fern-docs-generate
```

DNS note:

- Create the `CNAME` record for `docs.incidentflow.io` to the Fern-provided target in your Fern dashboard/domain settings.

### Notes on MCP schema fidelity

The `/mcp` endpoint is implemented as a custom ASGI proxy route and supports Streamable HTTP behavior (including SSE paths) that OpenAPI cannot fully encode.  
The OpenAPI document intentionally captures the stable HTTP + JSON-RPC contract and representative examples without inventing non-existent endpoints or transport behavior.

## Kubernetes Agent Tools

MCP Kubernetes tools resolve the connected cluster automatically through
`platform-api`, so normal usage does not require copying a `cluster_id`.
They keep `readOnlyHint=true`: platform command/result rows are transport and audit
bookkeeping for the read request and do not mutate the connected cluster.

Examples users can ask:

```text
Show Kubernetes namespaces
Show pods in production
List pods in namespace incidentflow-agent
Check failing pods in staging
```

Available read-only tools include:

- `k8s_connection_health`
- `k8s_cluster_overview`
- `k8s_namespace_overview`
- `k8s_rbac_check`
- `k8s_agent_status`
- `k8s_list_namespaces`
- `k8s_list_pods`
- `k8s_get_pod`
- `k8s_get_pod_logs`
- `k8s_list_events`
- `k8s_list_deployments`
- `k8s_list_services`
- `k8s_get_rollout_status`
- `k8s_show_unhealthy_pods`
- `k8s_analyze_workload`
- `k8s_describe_pod`
- `k8s_debug_pod`

Cluster selection behavior:

- If one cluster is connected in the current workspace, MCP selects it automatically.
- If multiple clusters are connected, pass `environment` (`production`, `staging`, or `dev`) or `cluster_name`.
- `cluster_id` is still accepted for internal debugging and direct control-plane tests, but should be omitted in normal user-facing prompts.

For local end-to-end testing, use OAuth/platform bearer auth for MCP so
`platform-api` can resolve the workspace and authorize Kubernetes command
dispatch. A static `INCIDENTFLOW_PAT` is useful for MCP-only auth smoke tests,
but it may not carry enough platform context for Kubernetes command dispatch.

### Grafana release status

Grafana work exists in source but is not part of the verified public tool surface yet. Do not
advertise or enable it for customers until the MCP registry imports cleanly, onboarding proves a
real datasource query with least-privilege credentials, and its manual/E2E acceptance suite passes.

### CI automation (GitHub Actions)

This repository includes a docs workflow at `.github/workflows/docs.yml`:

- On pull requests: generates OpenAPI, validates it, and runs `fern check`.
- On push to `main`: does the same validation and then runs `fern generate --docs --preview` if `FERN_TOKEN` is configured.
- On manual run (`workflow_dispatch`): set `publish_production=true` to publish to the custom domain.

Required repository secret for publishing previews:

- `FERN_TOKEN`

## VS Code MCP installer

After deploying this service behind an ingress (for example, `https://mcp.incidentflow.io`),
the app exposes a dynamic installer endpoint:

```bash
curl -fsSL https://mcp.incidentflow.io/install.sh | bash
```

The script auto-configures VS Code workspace `.vscode/mcp.json` with:
- `type: "http"`
- `url: https://<your-ingress-host>/mcp`

### Inspect the installer first (recommended)

You can inspect the installer before running it:

```bash
curl https://mcp.incidentflow.io/install.sh
```

or:

```bash
curl https://mcp.incidentflow.io/install.sh | less
```

### Dry run

Preview the changes without modifying your workspace:

```bash
curl https://mcp.incidentflow.io/install.sh | bash -s -- --dry-run
```

This prints the MCP configuration that would be written.

## Rate Limiting and Tool Guards

This server applies production-oriented protection in two layers:

1. HTTP transport-level limits (returns `429 Too Many Requests`)
2. MCP `tools/call` execution guards (structured JSON-RPC/MCP errors)

### Transport-level limits

Protected endpoints:
- `/mcp`
- auth endpoints if present (`/authorize`, `/token`, `/register`, `/oauth/register`)

Identity resolution order:
1. `workspace_id + user_id`
2. `client_id`
3. client IP

Plan metadata is passed through as raw identity metadata (for example `auth_context["plan"]` or `X-Plan`/`X-Plan-Tier` headers). Core OSS logic does not map or hardcode SaaS tiers.

Default OSS policy:
- unauthenticated: `20 req/min` per IP
- authenticated: `60 req/min` per principal

On transport limit hits, server returns HTTP `429` with:
- `Retry-After`
- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

### Tool-level limits and execution policy

For `tools/call` requests:
- authenticated default: `20 calls/min`
- expensive tools: `5 calls/min` per identity
- authenticated default concurrency: max `2` concurrent tool executions
- default timeout: `30s` (with optional per-tool override)

Bucket key selection is policy-driven (`ip` | `principal` | `workspace`) and resolved separately from identity.

Tool guard errors are returned as structured MCP/JSON-RPC errors with safe messages such as:
- `Rate limit exceeded for tool invocation`
- `Too many concurrent tool invocations`
- `Tool execution timed out`

### Expensive tools policy

Set expensive tools via:

```bash
EXPENSIVE_TOOLS=k8s_debug_pod,k8s_get_pod_logs,incident_thread_summary,memory_search_similar_incidents
```

### Redis requirement

Rate-limit and concurrency state is Redis-backed to work across multiple app replicas.
In local development:

```bash
REDIS_URL=redis://host.docker.internal:6379/0
```

The Docker Compose default expects the shared Redis from `incidentflow-platform`
to be running on host port `6379`. To run an MCP-only Redis fallback:

```bash
REDIS_URL=redis://:redis-dev-password@redis:6379/0 docker compose --profile local-deps up
```

The compose file also points `PLATFORM_API_BASE_URL` at the shared
`incidentflow-platform` API via `http://host.docker.internal:8000`. Override it
with `MCP_PLATFORM_API_BASE_URL=...` when needed.

### Metrics

Exposed on `/metrics` (Prometheus format):
- `mcp_http_requests_total`
- `mcp_http_rate_limited_total`
- `mcp_tool_calls_total`
- `mcp_tool_rate_limited_total`
- `mcp_tool_timeouts_total`
- `mcp_tool_concurrency_rejections_total`

For production MCP observability design, PromQL, and alert examples, see
`docs/observability.md`.

## Managed token introspection mode (recommended)

For SaaS deployments, prefer managed credentials from `platform-api` over a static `INCIDENTFLOW_PAT`.

Set these variables in `incidentflow-mcp`:

```bash
PLATFORM_API_BASE_URL=http://127.0.0.1:8000
PLATFORM_API_INTROSPECT_PATH=/api/v1/tokens/introspect
PLATFORM_API_TIMEOUT_SECONDS=5
```

In this mode, MCP verifies incoming bearer tokens via platform-api and receives
workspace/user/scope context from the introspection response. Token metadata such
as `last_used_at` is updated in platform-api during introspection.

Fallback behavior:
- If `PLATFORM_API_BASE_URL` is not set, MCP uses local auth (`INCIDENTFLOW_PAT` and/or local repo tokens).
- In production, at least one auth source must be configured (`PLATFORM_API_BASE_URL` or `INCIDENTFLOW_PAT`).

OAuth JWTs keep local signature, issuer, audience, expiry, not-before, and scope
validation. When both OAuth validation and `PLATFORM_API_BASE_URL` are configured,
MCP also calls `POST /oauth/introspect` for every locally valid OAuth request.
Successful status responses are not cached, so `POST /revoke` takes effect on the
next request. Introspection timeout or authority failure returns `503` (fail closed).
Outside local development, configure platform-api `OAUTH_INTROSPECTION_API_KEY`
and set the same value on MCP. `PLATFORM_API_INTERNAL_API_KEY` remains a
backward-compatible fallback, but a dedicated introspection key is preferred.

## Thread-aware Slack analysis

In SaaS/production, Slack tools are platform-backed. Users connect Slack in the
IncidentFlow UI/CLI, choose enabled channels, and invite the IncidentFlow bot to
those channels. MCP never receives the Slack bot token; it calls
`platform-api` internal Slack endpoints using the authenticated workspace
context from OAuth or managed token introspection. `SLACK_BOT_TOKEN` is a
legacy local-development fallback only and is ignored by production Slack tools
when platform mode is configured.

If a direct MCP client has not completed IncidentFlow OAuth, the transport can
return OAuth authorization required before a tool runs. If a tool runs without a
workspace-scoped auth context, Slack tools return `mcp_workspace_context_required`.

`slack_alerts_list` is thread-safe by default: it does not fetch Slack threads unless requested.
Use metadata mode for lightweight thread counts, and full mode when you need parsed engineer replies.

Example alert listing with full thread analysis:

```json
{
  "channel": "alerts",
  "limit": 20,
  "include_threads": true,
  "thread_mode": "full",
  "max_thread_replies": 20
}
```

Example direct thread read:

```json
{
  "channel_id": "C12345678",
  "message_ts": "1710000000.000100",
  "include_root": true,
  "max_replies": 50
}
```

Example SRE summary:

```json
{
  "channel_id": "C12345678",
  "thread_ts": "1710000000.000100",
  "alert_context": {
    "alert_name": "InstanceDown",
    "namespace": "cert-manager"
  }
}
```

Compact output shape:

```json
{
  "slack": {
    "channel_id": "C12345678",
    "channel_name": "alerts",
    "message_ts": "1710000000.000100",
    "thread_ts": "1710000000.000100",
    "permalink": "https://workspace.slack.com/archives/C123/p1710000000000100",
    "thread_permalink": "https://workspace.slack.com/archives/C123/p1710000000000100"
  },
  "thread": {
    "reply_count": 2,
    "last_reply_ts": "1710000010.000100",
    "participants": ["U123", "U456"],
    "replies": [
      {
        "ts": "1710000005.000100",
        "user": "U123",
        "text": "I think service: cert-manager lost endpoints",
        "contains_command": false,
        "contains_runbook_link": false,
        "contains_hypothesis": true,
        "contains_resolution": false
      }
    ],
    "analysis": {
      "summary": "1 hypothesis signal(s), 1 command(s)",
      "engineer_hypotheses": ["I think service: cert-manager lost endpoints"],
      "commands_found": ["kubectl get pods -n cert-manager"],
      "runbook_links": [
        {
          "url": "https://confluence.example/runbook/cert-manager",
          "label": "Runbook",
          "type": "runbook"
        }
      ],
      "resolution_signal": false,
      "resolution_confidence": "low"
    }
  }
}
```

Slack commands found in threads are extracted only for display. IncidentFlow MCP never executes
commands from Slack; remediation must be a separate approved action. Thread summaries are returned
without an implicit semantic-memory upsert; any future memory write must be a separate explicit
operation.

## Synthetic summary and memory release contract

`incident_summary` reads synthetic fixtures only in explicit development, demo, or test
environments. Every execution mode fails before job submission in production. The four
`memory_*` tools are also excluded from public submission and production `tools/list`; a stale
production call fails closed as an unknown tool until their backend contract is release-approved.

## `external_status_check` response modes

This tool reads public provider-status information. It does not connect to a customer's GitHub
account, organization, repositories, issues, or pull requests and is not a GitHub integration.

`external_status_check` supports two output modes:

- `response_mode=compact` (default): chat-safe summary for VS Code/Copilot rendering.
- `response_mode=full`: raw platform job payload for deep RCA analysis.

Polling behavior:
- If `check_id` is provided, MCP polls that existing `job_id` and does not create a new job.
- If `check_id` is omitted, MCP submits a new async job.
- OMS storage is caller opt-in. Only `persist_to_oms=true` requests storage, and the runner's
  deployment policy may still deny it. Omitted or explicit `false` can never be forced to `true`.
- The result's `persistence` object records `requested`, `effective`, and `stored` decisions.

Example (compact):

```json
{
  "providers": ["github"],
  "wait_for_result": true,
  "days_back": 30,
  "response_mode": "compact"
}
```

Example (full):

```json
{
  "providers": ["github"],
  "wait_for_result": true,
  "days_back": 30,
  "response_mode": "full"
}
```
