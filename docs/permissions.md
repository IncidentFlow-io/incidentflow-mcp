# IncidentFlow permissions (scopes)

How access to the MCP server and platform-api is granted, what each permission
allows, and copy-paste commands to check every one yourself.

## Credentials

| Credential | Looks like | Created in | Carries |
|---|---|---|---|
| OAuth access token | JWT | Claude / ChatGPT / Cursor sign-in | scopes the client requested at sign-in |
| Local static PAT (`INCIDENTFLOW_PAT`) | any string | local development only | full access, no workspace; **MCP refuses to start in production with it set**, and the Helm chart cannot deploy it |
| Personal API key | `if_pat_…` | App → Developers → API Keys → Personal keys | scopes you tick; acts as you |
| Workspace API key | `if_wst_…` | App → Developers → API Keys → Workspace keys | scopes you tick; acts as the key's creator in that workspace |
| Service account | OAuth client credentials → JWT | App → Developers → API Keys → Service accounts | the account's allowed scopes |

API keys are **shown once**, in the create response: Platform keeps only a
SHA-256 hash and the last four characters for display, so a lost key cannot be
recovered. Create a replacement and revoke the old one. Every new key **must
expire within 365 days** (7, 30, 60, 90, 180 days, 1 year, or a custom date up to a
year); keys created before this rule may still have no expiry.

How a request is checked:

1. **MCP** authenticates the bearer: OAuth JWT through JWKS, API keys through
   `POST /api/v1/tokens/introspect` (the result is cached for 30 s).
2. **MCP decides the permission** from the JSON-RPC method and tool (below).
3. **MCP never forwards a raw API key to Platform APIs.** It exchanges every
   accepted credential for a short-lived Platform token
   (`/internal/v1/oauth/token-exchange`). That token acts as the key's owner in
   the key's workspace, records the key in `act.credential_id`, and never outlives
   the key. A key whose owner is suspended or has left the workspace is refused.

## MCP permissions

The scopes form a hierarchy. A broader scope includes the narrower ones:

```
mcp:tools:write  ⇒  mcp:tools:run  ⇒  mcp:read
```

| Permission | UI label | Allows | Does not allow |
|---|---|---|---|
| `mcp:read` | Read MCP data | `initialize`, `ping`, `tools/list`, `resources/*`, `prompts/*`, the SSE `GET /mcp` stream | `tools/call` |
| `mcp:tools:run` | Run MCP tools | everything in `mcp:read`, plus `tools/call` for read-only tools: Kubernetes, Grafana, Logs, Argo CD, Slack, knowledge search, investigations, guides | tools that change data |
| `mcp:tools:write` | Run MCP write tools | everything in `mcp:tools:run`, plus tools that change data: `knowledge_upsert` | — |

Which tools are "write" is not a hand-kept list: it is every tool whose
`readOnlyHint` is false in `src/incidentflow_mcp/tools/registry.py`.
`investigation_continue` and `investigation_recheck` queue an investigation
pass but are annotated read-only, so `mcp:tools:run` covers them.

A credential that is valid but lacks the permission gets **HTTP 403**:

```
WWW-Authenticate: Bearer error="insufficient_scope", resource_metadata="…", scope="mcp:read mcp:tools:run"
{"detail": "Insufficient token scope: mcp:tools:run is required for tools/call:mcp_version", "error": "insufficient_scope", "required_scope": "mcp:read mcp:tools:run"}
```

A missing or invalid credential gets **HTTP 401**. Its challenge asks for
`mcp:read mcp:tools:run`, so a new OAuth client signs in with both.

### No compatibility mode

The model is always enforced, in every environment; there is no flag to relax
it. OAuth clients that connected before `mcp:tools:run` existed hold only
`mcp:read`: their tool calls get 403 `insufficient_scope`, and they must
reconnect to get `mcp:read mcp:tools:run`. API keys without **Run MCP tools**
must be replaced. Local development tokens follow the same rules.

## Platform-api permissions

| Permission | UI label | Allows | Credential | Notes |
|---|---|---|---|---|
| `incidents:read` | Read incidents | public incidents API: `GET /api/v1/incidents`, `GET /api/v1/incidents/{id}`, incident memory | personal key, service account, or user session | workspace keys are refused by this API |
| `signals:ingest` | Ingest signals | `POST /api/v1/webhooks/alertmanager` | workspace or personal key | used by Alertmanager receivers |
| `graph:read` | Read graph | — | — | **Reserved.** Not checked by any API yet. |
| `integrations:read` | Read integrations | — (for API keys) | — | **Reserved for API keys.** Used only inside Platform tokens minted for MCP. |

## Check it yourself

Local stack: platform-api on `:8000`, app on `:3001`, MCP on `:8001`:

```bash
cd incidentflow-mcp && uv run incidentflow-mcp serve --reload --host 127.0.0.1 --port 8001
```

Create test keys in the app (Developers → API Keys → Workspace keys → Create API key):

| Key | Tick | Expect |
|---|---|---|
| `KEY_READ` | Read MCP data | `read` |
| `KEY_RUN` | Run MCP tools | `run` |
| `KEY_WRITE` | Run MCP write tools | `write` |
| `KEY_NONE` | Ingest signals only | `none` |

### Everything at once

```bash
IF_TOKEN="$KEY_RUN" EXPECT=run bash scripts/check_permissions.sh
```

Run it once per key with its `EXPECT`. Every MCP row must say `OK`.

### One permission at a time

Common setup:

```bash
export MCP=http://127.0.0.1:8001/mcp
```
```bash
export H_ACCEPT='Accept: application/json, text/event-stream'
```

**`mcp:read`: discovery works, tool calls do not.**
Expect `200` with a `KEY_READ` key:
```bash
curl -s -o /dev/null -w '%{http_code}\n' "$MCP" -H "Authorization: Bearer $KEY_READ" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```
Expect `403` and `error="insufficient_scope"` in the headers:
```bash
curl -si "$MCP" -H "Authorization: Bearer $KEY_READ" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"mcp_version","arguments":{}}}' | grep -iE '^HTTP|www-authenticate'
```

**`mcp:tools:run`: read-only tools work, write tools do not.**
Expect `200` with a `KEY_RUN` key:
```bash
curl -s -o /dev/null -w '%{http_code}\n' "$MCP" -H "Authorization: Bearer $KEY_RUN" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"mcp_version","arguments":{}}}'
```
Expect `403`, with `scope="mcp:read mcp:tools:run mcp:tools:write"` in the challenge:
```bash
curl -si "$MCP" -H "Authorization: Bearer $KEY_RUN" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"knowledge_upsert","arguments":{"title":"t","text":"t","document_type":"knowledge","dry_run":true}}}' | grep -iE '^HTTP|www-authenticate'
```

**`mcp:tools:write`: write tools work.**
Expect `200`. `dry_run` stores nothing:
```bash
curl -s -o /dev/null -w '%{http_code}\n' "$MCP" -H "Authorization: Bearer $KEY_WRITE" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"knowledge_upsert","arguments":{"title":"t","text":"t","document_type":"knowledge","dry_run":true}}}'
```

**No MCP permission: valid key, no MCP access.**
Expect `403` even for `tools/list`:
```bash
curl -s -o /dev/null -w '%{http_code}\n' "$MCP" -H "Authorization: Bearer $KEY_NONE" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

**No or invalid credential.**
Expect `401` and `scope="mcp:read mcp:tools:run"` in the challenge:
```bash
curl -si "$MCP" -H "$H_ACCEPT" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | grep -iE '^HTTP|www-authenticate'
```

**Introspection: what platform-api sees for a key.**
Shows `scopes`, `workspace_id`, and `token_type`:
```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tokens/introspect -H "Authorization: Bearer $KEY_RUN" -H 'Content-Type: application/json' -d '{}'
```

**`incidents:read`: needs a personal key with Read incidents.**
Expect `200`; a workspace key gets `401`:
```bash
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8000/api/v1/incidents?limit=1' -H "Authorization: Bearer $PERSONAL_KEY_INCIDENTS"
```

**`signals:ingest`: needs a key with Ingest signals.**
The empty alert list is rejected only after the permission check, so nothing is
stored. Expect `422` ("must contain alerts") with the permission, `403` without it:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/v1/webhooks/alertmanager -H "Authorization: Bearer $KEY_NONE" -H 'Content-Type: application/json' -d '{"version":"4","status":"firing","alerts":[]}'
```

### Metrics

| Metric | Meaning |
|---|---|
| `mcp_scope_decisions_total{required_scope, decision, auth_method}` | every permission decision: `allow` or `deny` |
| `mcp_auth_failures_total{reason="insufficient_scope"}` | 403s |

## Known limits

- **Workspace keys act as their creator.** If the creator leaves the workspace,
  MCP refuses the key at exchange time. Introspection alone (MCP without token
  exchange) does not check this.
- **Introspection is cached for 30 s,** so a revoked key works for up to 30 s more
  on an MCP instance that already saw it.
