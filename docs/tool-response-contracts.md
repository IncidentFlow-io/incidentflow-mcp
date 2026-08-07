# IncidentFlow MCP Tool Response Contracts

Every IncidentFlow MCP tool returns the **same canonical response envelope**. The
tool-specific payload always lives inside `data`; versioning, status, correlation
and metadata live in fixed envelope fields.

> See [api-versioning.md](./api-versioning.md) for the versioning model, the
> schema-ID catalog and the deprecation policy.

## Common Envelope

Every tool response — success and error — has exactly these eight keys:

```json
{
  "api_version": "v1",
  "schema_version": "1.0",
  "schema_id": "incidentflow.external-status-check.response",
  "status": "success",
  "request_id": "req_123",
  "data": {},
  "error": null,
  "meta": {
    "generated_at": "2026-08-07T09:29:30Z",
    "truncated": false,
    "warnings": []
  }
}
```

| Field | Meaning |
|---|---|
| `api_version` | HTTP/MCP API version (`v1`). |
| `schema_version` | Response-structure version (`1.0`). Independent of `service_version`. |
| `schema_id` | Stable response schema id for this tool (`incidentflow.<tool>.response`). |
| `status` | The overall call result: `success` or `error`. |
| `request_id` | Correlation id (`req_...`), also usable in logs/traces. |
| `data` | Tool-specific payload on success; `null` on error. |
| `error` | `null` on success; the error object on error. |
| `meta` | `generated_at`, `truncated`, and `warnings[]`. |

## Error Object

```json
{
  "status": "error",
  "data": null,
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "Invalid job_id format",
    "retryable": false,
    "details": { "field": "job_id", "expected": "uuid" }
  }
}
```

Every error uses one of the ten canonical [error codes](#canonical-error-codes).
The **same code** appears in:

- `structuredContent.error.code`;
- the MCP `isError` outcome (`isError=true` for every tool error);
- application logs (`record_tool_failure`);
- metrics / traces.

There is never a case where the client sees `INVALID_ARGUMENT` while middleware
records `TOOL_ERROR`.

## Canonical Error Codes

| Code | Default `retryable` | Typical cause |
|---|---|---|
| `INVALID_ARGUMENT` | false | bad/rejected tool arguments (incl. schema validation) |
| `UNAUTHENTICATED` | false | missing/invalid credentials (401) |
| `PERMISSION_DENIED` | false | authenticated but not allowed (403) |
| `NOT_FOUND` | false | resource does not exist (404) |
| `CONFLICT` | false | state conflict (409) |
| `RATE_LIMITED` | true | upstream/gateway rate limit (429) |
| `INTEGRATION_UNAVAILABLE` | true | integration not connected / unreachable |
| `UPSTREAM_ERROR` | true | upstream 5xx or transport error |
| `TIMEOUT` | true | upstream timeout |
| `INTERNAL_ERROR` | false | unexpected server-side failure |

Codes are defined once in `incidentflow_mcp.tools.contracts.ErrorCode`. Exception
→ code mapping lives in `incidentflow_mcp.mcp.errors.map_exception`.

## Where the contract is enforced

- **Builders** — `success_envelope` / `error_envelope` in `tools/contracts.py`.
- **Wrapper** — `mcp/compatibility/fastmcp_contracts.py` wraps every tool result
  (a raw handler dict becomes `data`; exceptions and inline `tool_error` signals
  become error envelopes with `isError=true`).
- **Output schemas** — each tool publishes a precise inline JSON Schema
  (`build_output_schema`) instead of a generic `{additionalProperties: true}`
  object. The low-level MCP server validates success `structuredContent` against it.

## Field-naming rules (normalization)

- The **call result** is always `status` (`success` / `error`) at the envelope level.
- **Integration state** is `healthy: bool` inside `data` (e.g. `k8s_agent_status`,
  `argocd_connection_health`) — never a second `ok` / `agent_online` field.
- Domain-specific outcomes (e.g. the external-status provider check) use a distinct
  name such as `check_status`, never `execution_status` overloaded against `status`.
- Tool-specific payload is always inside `data`, never at the top level.

## Data schemas

Per-tool `data` schemas live in `incidentflow_mcp.tools.data_schemas`. Each tool has
its **own** schema — there is no single blanket `additionalProperties: true` schema
shared across tools. Tools whose payload we fully own are strict
(`additionalProperties: false`, e.g. `mcp_version`, `k8s_agent_status`); tools that
pass an upstream payload through (`argocd_connection_health`,
`public_knowledge_search`, `external_status_check`) declare their known fields but
tolerate upstream additions so a live response never fails output validation.

## Regenerating schema files

```bash
uv run python scripts/generate_tool_schemas.py
```

Writes `schemas/tools/incidentflow.common.error.schema.json`,
`schemas/tools/incidentflow.common.envelope.schema.json`, and one
`incidentflow.<tool>.response.schema.json` per registered tool.
