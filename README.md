# incidentflow-mcp

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

The IncidentFlow MCP server is open-source under the MIT License.

IncidentFlow Cloud platform and hosted services are proprietary.

## Connect hosted MCP clients

Use these steps when the MCP server is already deployed behind HTTPS, for
example:

- Development: `https://mcp-dev.incidentflow.io/mcp`
- Production: `https://mcp.incidentflow.io/mcp`

The hosted server uses OAuth 2.1 with PKCE. Clients should register the MCP
server as a Streamable HTTP MCP server, run the OAuth login flow, and then call
tools with the issued access token.

Default MCP scopes:

- `mcp:read` — connect to the MCP resource and read MCP resources/metadata.
- `mcp:tools:run` — execute MCP tools.

Do not request `admin` for normal hosted MCP clients. `openid`, `email`, and
`profile` are only for explicit OpenID Connect identity flows; MCP tool access
does not require them.

### Quick HTTP checks

Check that the MCP endpoint is protected and advertises OAuth metadata:

```bash
curl -i https://mcp-dev.incidentflow.io/mcp
```

Expected unauthenticated response:

```http
HTTP/2 401
www-authenticate: Bearer resource_metadata="https://mcp-dev.incidentflow.io/.well-known/oauth-protected-resource"
```

Check authorization server metadata:

```bash
curl -sS https://mcp-dev.incidentflow.io/.well-known/oauth-authorization-server | jq
```

Expected fields include:

```json
{
  "issuer": "https://mcp-dev.incidentflow.io",
  "authorization_endpoint": "https://mcp-dev.incidentflow.io/authorize",
  "token_endpoint": "https://mcp-dev.incidentflow.io/token",
  "registration_endpoint": "https://mcp-dev.incidentflow.io/register",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "token_endpoint_auth_methods_supported": ["none"],
  "code_challenge_methods_supported": ["S256"]
}
```

For a normal MCP OAuth flow that requests only MCP scopes, the `/token` response
must omit `id_token` completely. It must not return `"id_token": null`.

Expected successful token shape:

```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "mcp:read mcp:tools:run",
  "refresh_token": "..."
}
```

`id_token` is only expected for OpenID Connect flows that request the `openid`
scope.

### Claude Code

Add the development MCP server:

```bash
claude mcp add --transport http incidentflow-dev https://mcp-dev.incidentflow.io/mcp
```

Authenticate with OAuth:

```bash
claude mcp login incidentflow-dev
```

Claude Code opens the browser. If the browser does not open, paste the printed
authorization URL into a browser, complete login/consent, and return to the CLI
when prompted.

Verify the server is configured:

```bash
claude mcp list
claude mcp get incidentflow-dev
```

Remove or re-authenticate when needed:

```bash
claude mcp logout incidentflow-dev
claude mcp remove incidentflow-dev
```

Production uses the same commands with a different name and URL:

```bash
claude mcp add --transport http incidentflow https://mcp.incidentflow.io/mcp
claude mcp login incidentflow
```

### Codex CLI

Add the development MCP server:

```bash
codex mcp add incidentflow-dev \
  --url https://mcp-dev.incidentflow.io/mcp \
  --oauth-resource https://mcp-dev.incidentflow.io/mcp
```

Authenticate with OAuth and request the MCP scopes:

```bash
codex mcp login incidentflow-dev --scopes mcp:read,mcp:tools:run
```

Verify the server is configured:

```bash
codex mcp list
codex mcp get incidentflow-dev
codex mcp get incidentflow-dev --json
```

Remove or re-authenticate when needed:

```bash
codex mcp logout incidentflow-dev
codex mcp remove incidentflow-dev
```

Production uses the same commands with a different name and URL:

```bash
codex mcp add incidentflow \
  --url https://mcp.incidentflow.io/mcp \
  --oauth-resource https://mcp.incidentflow.io/mcp

codex mcp login incidentflow --scopes mcp:read,mcp:tools:run
```


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

Examples users can ask:

```text
Show Kubernetes namespaces
Show pods in production
List pods in namespace incidentflow-agent
Check failing pods in staging
```

Available read-only tools include:

- `k8s_agent_status`
- `k8s_connection_health`
- `k8s_cluster_overview`
- `k8s_namespace_overview`
- `k8s_list_namespaces`
- `k8s_list_pods`
- `k8s_show_unhealthy_pods`
- `k8s_get_pod`
- `k8s_describe_pod`
- `k8s_debug_pod`
- `k8s_get_pod_logs`
- `k8s_list_events`
- `k8s_list_deployments`
- `k8s_get_rollout_status`
- `k8s_analyze_workload`
- `k8s_list_services`
- `k8s_rbac_check`

To inspect the canonical tool metadata exported by this package:

```bash
uv run incidentflow-mcp tools list --json-output
```

Agents can call `incidentflow_capabilities` for a deterministic in-band
inventory of the 39 operational tools grouped by category, without search
ranking or result limits.

Cluster selection behavior:

- If one cluster is connected in the current workspace, MCP selects it automatically.
- If multiple clusters are connected, pass `environment` (`production`, `staging`, or `dev`) or `cluster_name`.
- `cluster_id` is still accepted for internal debugging and direct control-plane tests, but should be omitted in normal user-facing prompts.

For local end-to-end testing, use OAuth/platform bearer auth for MCP so
`platform-api` can resolve the workspace and authorize Kubernetes command
dispatch. A static `INCIDENTFLOW_PAT` is useful for MCP-only auth smoke tests,
but it may not carry enough platform context for Kubernetes command dispatch.

### Grafana MCP tools (read-only)

The MCP package now ships Grafana read tools over the /mcp transport.
All tools are read-only and rely on platform-api for allow-lists, PromQL guardrails, and label sanitization.

MCP does not connect to Grafana directly and does not store a Grafana service
account token. The request path is:

```text
MCP grafana_* tool
  -> platform-api /internal/integrations/grafana/*
  -> platform-api decrypts stored Grafana SA token
  -> Grafana API / datasource proxy
```

The MCP-to-platform call uses `PLATFORM_API_INTERNAL_API_KEY` as
`X-Internal-Api-Key`. The user-facing MCP bearer token is only used to resolve
and authorize the workspace. If the token carries a workspace scope, an explicit
different `workspace_id` is rejected with `workspace_scope_mismatch`.

Available tools:
- grafana_list_dashboards
- grafana_get_dashboard
- grafana_extract_panel_queries
- grafana_metrics_query
- grafana_metrics_query_range
- analyze_dashboard_health

Typical workflows:

```text
List approved dashboards: grafana_list_dashboards
Read dashboard metadata: grafana_get_dashboard {"dashboard_uid":"dns"}
Extract dashboard queries: grafana_extract_panel_queries {"dashboard_uid":"dns"}
Run instant PromQL: grafana_metrics_query {"datasource_uid":"prometheus", "query":"sum(rate(http_requests_total[5m]))"}
Run range PromQL: grafana_metrics_query_range {"datasource_uid":"prometheus", "query":"sum(rate(http_requests_total[5m]))", "start":"now-1h", "end":"now", "step":"30s"}
Inspect full dashboard health: analyze_dashboard_health {"dashboard_uid":"dns", "start":"now-6h", "end":"now", "step":"60s"}
```

Production/dev prerequisites:

- The workspace must have a connected Grafana integration in platform-api.
- At least one dashboard must be saved in `grafana_allowed_dashboards`.
- MCP must have `PLATFORM_API_BASE_URL` pointing to platform-api and
  `PLATFORM_API_INTERNAL_API_KEY` configured.
- MCP should have either a workspace-scoped token or
  `MCP_DEFAULT_WORKSPACE_ID`/`INCIDENTFLOW_WORKSPACE_ID` configured for local
  smoke tests.

### Local Grafana smoke stack

For local MCP testing, `docker-compose.yml` includes a `grafana-smoke` profile with:

- Grafana on `http://localhost:3000`
- Prometheus on `http://localhost:9090`
- node-exporter on `http://localhost:9100`
- Grafana.com dashboard `1860` imported automatically
- a one-shot helper that creates a Grafana service-account token for platform-api

Start the stack:

```bash
docker compose --profile grafana-smoke up -d prometheus node-exporter grafana grafana-dashboard-1860
```

Create and print a Grafana service-account token:

```bash
docker compose --profile grafana-smoke run --rm grafana-sa-token
```

Use the printed `GRAFANA_SA_TOKEN` with platform-api. For local-only testing,
platform-api must allow private/loopback Grafana URLs:

```bash
GRAFANA_ALLOW_PRIVATE_URLS_FOR_DEV=true \
AGENT_GATEWAY_URL=ws://host.docker.internal:8002/agents/ws \
uv run uvicorn platform_api.main:app --reload --app-dir src --host 0.0.0.0 --port 8000
```

Connect platform-api to local Grafana with the service-account token:

```bash
curl -sS -X POST http://localhost:8000/api/v1/integrations/grafana/connect \
  -H "Authorization: Bearer $PLATFORM_USER_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{\"grafana_url\":\"http://localhost:3000\",\"grafana_token\":\"$GRAFANA_SA_TOKEN\",\"default_datasource_uid\":\"prometheus\"}"
```

Then discover dashboards and save dashboard `1860` to the workspace allow-list:

```bash
curl -sS http://localhost:8000/api/v1/integrations/grafana/dashboards \
  -H "Authorization: Bearer $PLATFORM_USER_TOKEN"

curl -sS -X POST http://localhost:8000/api/v1/integrations/grafana/allowed-dashboards \
  -H "Authorization: Bearer $PLATFORM_USER_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"dashboards":[{"dashboard_uid":"rYdddlPWk","title":"Node Exporter Full","folder":"","datasource_uid":"prometheus","enabled":true}]}'
```

Finally call MCP through the normal `/mcp` transport:

```bash
curl -sS http://127.0.0.1:8001/mcp \
  -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"grafana_list_dashboards","arguments":{}}}'
```

### Creating an MCP PAT with workspace scope

Grafana (and Kubernetes) tools resolve the workspace via the MCP bearer token.
A token without a `workspace_id` will return empty results for any workspace-scoped tool.

**Step 1 — find the workspace ID:**

```bash
# from the platform-api database
psql $DATABASE_URL -c "SELECT id, name FROM workspaces;"
# or from the platform-api REST API (when authenticated)
curl -sS http://localhost:8000/api/v1/workspaces \
  -H "Authorization: Bearer $PLATFORM_USER_TOKEN"
```

**Step 2 — create a PAT bound to that workspace:**

```bash
uv run incidentflow-mcp token create \
  --name "local-dev" \
  --workspace-id "<workspace-uuid>"
```

The `--workspace-id` flag was added specifically to avoid the empty-result
issue where tokens created without it returned 0 dashboards from
`grafana_list_dashboards`.

**Step 3 — wire the token into `.vscode/mcp.json`:**

```json
{
  "servers": {
    "incidentflow-local": {
      "url": "http://127.0.0.1:8001/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer <token printed by token create>"
      }
    }
  }
}
```

**Revoke old tokens** (any previously created without `--workspace-id`):

```bash
uv run incidentflow-mcp token list      # find token IDs
uv run incidentflow-mcp token revoke <token_id>
```

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
EXPENSIVE_TOOLS=incident_graph_build,large_correlation,slack_thread_mining,github_org_search
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
commands from Slack; remediation must be a separate approved action.

## `external_status_check` response modes

`external_status_check` supports two output modes:

- `response_mode=compact` (default): chat-safe summary for VS Code/Copilot rendering.
- `response_mode=full`: raw platform job payload for deep RCA analysis.

Polling behavior:
- If `check_id` is provided, MCP polls that existing `job_id` and does not create a new job.
- If `check_id` is omitted, MCP submits a new async job.

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

---

## Fixes and validation log

### Bug: `grafana_metrics_query_range` returned HTTP 500

**Root cause:** `start` / `end` values such as `now-15m` and `now` were forwarded
verbatim to Prometheus via the Grafana datasource proxy.  Prometheus only accepts
Unix timestamps or RFC 3339 strings — it does not support Grafana-style relative
expressions.

**Fix:** Added `_resolve_timestamp()` to
`src/platform_api/infra/grafana/client.py`.  The function converts
`now`, `now-<N><unit>` (ms / s / m / h / d / w) to integer Unix timestamps
before the Grafana proxy call.  RFC 3339 strings and plain Unix timestamps pass
through unchanged.

```python
# examples
_resolve_timestamp("now")      → "1782836058"
_resolve_timestamp("now-15m")  → "1782835158"
_resolve_timestamp("now-6h")   → "1782814458"
_resolve_timestamp("2026-01-01T00:00:00Z")  → "2026-01-01T00:00:00Z"  # passthrough
```

### Bug: `grafana_list_dashboards` returned 0 results

**Root cause:** Tokens created with `incidentflow-mcp token create` did not
accept a `--workspace-id` flag, so every PAT was stored with
`workspace_id: null`.  The platform-api `/internal/integrations/grafana/allowed-dashboards`
endpoint requires a workspace UUID to look up the allow-list.

**Fix:** Added `--workspace-id UUID` option to `token create` and updated
`token list` to show the workspace column.

### Grafana tools — end-to-end smoke test results

All 7 Grafana MCP tools verified against the local smoke stack
(`docker compose --profile grafana-smoke`, dashboard `Node Exporter Full` / uid `rYdddlPWk`):

| Tool | Result |
|---|---|
| `grafana_list_dashboards` | ✅ 1 dashboard returned |
| `grafana_get_dashboard` | ✅ 31 panels, metadata correct |
| `grafana_extract_panel_queries` | ✅ 284 PromQL expressions extracted |
| `grafana_metrics_query` | ✅ instant query — 2 series, both UP |
| `grafana_metrics_query_range` | ✅ 16 samples over 15 m — fixed by `_resolve_timestamp` |

```
up{} — last 15 min (step 60s)
1.0 ┤ ●──●──●──●──●──●──●──●──●──●──●──●──●──●──●──● node-exporter:9100
    │ ○──○──○──○──○──○──○──○──○──○──○──○──○──○──○──○ prometheus:9090
0.0 ┤
    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬
      -15  -14  -13  -12  -11  -10  -9  -8  -7  -6  -5  -4  -3  -2  -1   0 min
```
| `analyze_dashboard_health` | ✅ 284 panels, 0 errors, 124 rejected by guardrails |
| `analyze_dns_dashboard` | ✅ 0 DNS panels (expected — Node Exporter is not a DNS dashboard) |
