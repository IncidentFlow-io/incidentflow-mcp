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
REDIS_URL=redis://:redis-dev-password@127.0.0.1:6379/0
```

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
