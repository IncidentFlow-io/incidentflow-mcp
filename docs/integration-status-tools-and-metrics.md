# Integration Status Tools and Metrics

This runbook covers the workspace-aware IncidentFlow MCP status tools and the
integration metrics added across `incidentflow-mcp` and `platform-api`.

## IncidentFlow Tool Review - Auth Status Contract Improvements

Use `incidentflow_auth_status` as the canonical quick check for the active MCP
principal and workspace scope. The response contract should stay intentionally
small, safe to paste into support threads, and aligned with the runtime tool
registry.

Contract expectations:

- Always include `authenticated`, `authMethod`, `client`, `user`, `workspace`,
  `permissions`, `connectedIntegrations`, `availableToolGroups`, and
  `environment`.
- Include `workspace.id` in addition to `workspace.slug`, `workspace.name`, and
  `workspace.role` so support can distinguish workspaces with similar names.
- Use `connectedIntegrations` only for workspace-backed integrations with
  `status="connected"` and `source="workspace"`.
- Use `availableToolGroups` for the effective MCP surface visible to the
  principal. It always starts with `platform` and then includes connected
  integration groups, including development fallback groups when active.
- Keep the tool read-only and never return access tokens, refresh tokens,
  cookies, raw OAuth claims, authorization headers, or provider credentials.

When reviewing docs or client examples, prefer the live
`incidentflow_capabilities` inventory over stale generated docs or cached
submission metadata. The runtime inventory is the source of truth for which
tools are currently exposed.

## MCP Tools

### `incidentflow_auth_status`

Shows the authenticated IncidentFlow principal used by the current MCP request.

Use it in Codex/ChatGPT with natural language:

```text
Покажи мой статус авторизации в IncidentFlow
```

JSON-RPC call:

```bash
export MCP_URL="http://127.0.0.1:8001/mcp"
export INCIDENTFLOW_MCP_TOKEN="<redacted>"

curl -sS "$MCP_URL" \
  -H "Authorization: Bearer $INCIDENTFLOW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "incidentflow_auth_status",
      "arguments": {}
    }
  }' | jq .
```

If the MCP response wraps the tool output as text, parse the inner JSON:

```bash
curl -sS "$MCP_URL" \
  -H "Authorization: Bearer $INCIDENTFLOW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "incidentflow_auth_status",
      "arguments": {}
    }
  }' | jq -r '.result.content[0].text' | jq .
```

Expected shape:

```json
{
  "authenticated": true,
  "authMethod": "oauth",
  "client": {
    "name": "OAuth MCP client",
    "type": "mcp"
  },
  "user": {
    "email": "demo@example.com"
  },
  "workspace": {
    "id": "ws_123",
    "slug": "demo",
    "name": "Demo Workspace",
    "role": "owner"
  },
  "permissions": [
    "workspace.read",
    "integrations.read",
    "integrations.manage"
  ],
  "connectedIntegrations": [
    "kubernetes",
    "grafana",
    "argocd"
  ],
  "availableToolGroups": [
    "platform",
    "kubernetes",
    "grafana",
    "argocd"
  ],
  "environment": "dev"
}
```

The tool must not return access tokens, refresh tokens, cookies, or raw OAuth
claims.

### `incidentflow_integrations_status`

Shows which integrations are connected for the active workspace.

Use it in Codex/ChatGPT with natural language:

```text
Покажи статус интеграций IncidentFlow
```

JSON-RPC call:

```bash
curl -sS "$MCP_URL" \
  -H "Authorization: Bearer $INCIDENTFLOW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "incidentflow_integrations_status",
      "arguments": {}
    }
  }' | jq -r '.result.content[0].text' | jq .
```

Example output:

```json
{
  "kubernetes": {
    "status": "connected",
    "source": "workspace",
    "displayName": "kind-local",
    "resourceCount": 1
  },
  "grafana": {
    "status": "connected",
    "source": "workspace",
    "displayName": "Grafana",
    "resourceCount": 2
  },
  "slack": {
    "status": "not_connected",
    "source": null,
    "displayName": "Slack",
    "message": "Slack is not connected for the current workspace.",
    "actions": [
      {
        "type": "open_url",
        "label": "Connect Slack",
        "url": "https://app-dev.incidentflow.io/integrations"
      },
      {
        "type": "open_url",
        "label": "Read setup guide",
        "url": "https://incidentflow.io/docs/integrations/slack"
      }
    ]
  },
  "argocd": {
    "status": "connected",
    "source": "workspace",
    "displayName": "Argo CD",
    "resourceCount": 37
  }
}
```

For Kubernetes shared development fallback, the status includes a visible
warning:

```json
{
  "kubernetes": {
    "status": "connected",
    "source": "shared_dev",
    "workspaceIntegration": "not_connected",
    "warning": "Using the shared IncidentFlow development Kubernetes agent.",
    "workspaceActions": [
      {
        "type": "open_url",
        "label": "Connect Kubernetes",
        "url": "https://app-dev.incidentflow.io/integrations"
      },
      {
        "type": "open_url",
        "label": "Read setup guide",
        "url": "https://incidentflow.io/docs/integrations/kubernetes"
      }
    ],
    "effectiveConnection": {
      "type": "shared_dev_agent",
      "cluster": "incidentflow-dev",
      "environment": "dev"
    }
  }
}
```

`actions` are included only for integrations with `status="not_connected"`.
`workspaceActions` appears for Kubernetes shared development fallback: the tool
can use the shared dev agent, but the workspace still has no Kubernetes
integration configured.

## Direct Platform Status Check

The MCP integration status tool prefers the platform internal workspace status
endpoint. This is useful for debugging without going through MCP.

```bash
export PLATFORM_API_URL="http://localhost:8000"
export PLATFORM_API_INTERNAL_TOKEN="<redacted>"
export WORKSPACE_ID="<workspace-uuid>"

curl -sS "$PLATFORM_API_URL/internal/integrations/status/workspace?workspace_id=$WORKSPACE_ID" \
  -H "X-Internal-Api-Key: $PLATFORM_API_INTERNAL_TOKEN" \
  | jq .
```

Expected shape:

```json
{
  "kubernetes": {
    "clusters": []
  },
  "grafana": {
    "connected": true,
    "status": "connected"
  },
  "slack": {
    "connected": false,
    "status": "not_connected"
  },
  "argocd": {
    "connected": true,
    "status": "connected",
    "application_count": 37
  }
}
```

## Metrics

### MCP Metric: Integration Guard Decisions

Metric:

```text
mcp_integration_guard_total{tool,integration,result,environment}
```

Labels:

- `tool`: MCP tool name, for example `grafana_list_dashboards`.
- `integration`: `kubernetes`, `grafana`, `slack`, or `argocd`.
- `result`: `workspace`, `shared_dev`, or `not_connected`.
- `environment`: `dev`, `staging`, or `production`.

Raw scrape check:

```bash
curl -sS "http://127.0.0.1:8001/metrics" \
  | grep '^mcp_integration_guard_total'
```

Prometheus text format is not JSON, so `curl /metrics | jq .` is not valid.
Use Prometheus HTTP API when you want `curl | jq`.

Prometheus API examples:

```bash
export PROM_URL="http://localhost:9090"

curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(mcp_integration_guard_total{result="workspace"}[24h]))' \
  | jq .
```

Top integration tools blocked because integration is not connected:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=topk(10, sum by (tool, integration) (increase(mcp_integration_guard_total{result="not_connected"}[24h])))' \
  | jq .
```

Shared development fallback usage:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (tool) (increase(mcp_integration_guard_total{result="shared_dev"}[24h]))' \
  | jq .
```

### Platform Metric: Integration Lifecycle Actions

Metric:

```text
incidentflow_platform_api_integration_actions_total{integration,action,result}
```

This tracks successful lifecycle events:

- `integration="kubernetes", action="connect"` when an agent registers.
- `integration="grafana", action="connect|disconnect"`.
- `integration="slack", action="connect|disconnect"`.
- `integration="argocd", action="connect|disconnect"`.

Raw scrape check:

```bash
curl -sS "http://localhost:8000/metrics" \
  | grep '^incidentflow_platform_api_integration_actions_total'
```

Installs/connects in the last 7 days:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(incidentflow_platform_api_integration_actions_total{action="connect",result="success"}[7d]))' \
  | jq .
```

Disconnects in the last 7 days:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(incidentflow_platform_api_integration_actions_total{action="disconnect",result="success"}[7d]))' \
  | jq .
```

### Platform Metric: Observed Integration Statuses

Metric:

```text
incidentflow_platform_api_integration_status_observed_total{integration,status}
```

This counts statuses observed by the internal workspace status endpoint used by
MCP.

Raw scrape check:

```bash
curl -sS "http://localhost:8000/metrics" \
  | grep '^incidentflow_platform_api_integration_status_observed_total'
```

Observed connected vs not connected over 24 hours:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration, status) (increase(incidentflow_platform_api_integration_status_observed_total[24h]))' \
  | jq .
```

## What These Metrics Answer

### What users are using

Use existing MCP tool metrics:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=topk(10, sum by (tool) (increase(mcp_tool_requests_total{traffic_type="business"}[24h])))' \
  | jq .
```

### Which integrations work best

Workspace-backed success:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(mcp_integration_guard_total{result="workspace"}[24h]))' \
  | jq .
```

Blocked usage:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(mcp_integration_guard_total{result="not_connected"}[24h]))' \
  | jq .
```

Rough effectiveness ratio:

```bash
curl -G -sS "$PROM_URL/api/v1/query" \
  --data-urlencode 'query=sum by (integration) (increase(mcp_integration_guard_total{result="workspace"}[24h])) / clamp_min(sum by (integration) (increase(mcp_integration_guard_total[24h])), 1)' \
  | jq .
```

## Limitations

Counters answer event questions such as "how many connects happened in the last
7 days" or "which tools were blocked today".

They are not a perfect source of truth for "how many workspaces currently have
Grafana installed" because reconnect/upsert, backfills, and historical state can
make `connect - disconnect` drift. For exact current installation counts, add a
DB-backed aggregate gauge on `/metrics`, for example:

```text
incidentflow_platform_api_integrations_connected_current{integration}
```

That gauge should be computed from platform database tables, without
`workspace_id` labels.
