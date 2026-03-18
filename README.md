# incidentflow-mcp

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

The IncidentFlow MCP server is open-source under the MIT License.

IncidentFlow Cloud platform and hosted services are proprietary.

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
