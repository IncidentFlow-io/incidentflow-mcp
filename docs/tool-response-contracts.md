# IncidentFlow MCP Tool Response Contracts

IncidentFlow MCP tool responses use an additive v1 response contract. Existing
tool-specific fields remain at the top level, while every dictionary response is
stamped with common contract metadata.

## Common Envelope

Every dictionary response should include:

```json
{
  "schemaVersion": "v1",
  "schemaId": "argocd.get-last-operation",
  "ok": true,
  "warnings": []
}
```

Common optional fields:

- `status`: machine-readable status such as `failed`, `permission_denied`, or `ok`.
- `source`: provenance and freshness metadata for integration-backed tools.
- `truncated`: whether the payload is partial because of limits or compact mode.
- `error`: structured error object with `code`, `message`, optional `http_status`,
  and optional `details`.

## Reserved Keys

The following top-level keys are reserved by the global response contract:

- `schemaVersion`
- `schemaId`
- `warnings`

The runtime contract stamping is additive and uses `setdefault`, so it will not
overwrite an existing value. New tools should not use these keys for any
tool-specific meaning.

## Recommended Status Values

The v1 model keeps `status` as a string for compatibility, but tools should use
these values where possible:

- `ok`
- `failed`
- `partial`
- `truncated`
- `not_connected`
- `not_found`
- `permission_denied`
- `upstream_unavailable`

Future stricter models may convert these into enums for new or migrated tools.

## Preferred Error Shape

The v1 model accepts legacy string and dictionary errors, but new tools should
return this preferred object shape:

```json
{
  "ok": false,
  "status": "permission_denied",
  "error": {
    "code": "integration_permission_denied",
    "message": "The integration token is missing the required read permission.",
    "http_status": 403,
    "details": {
      "integration": "argocd",
      "operation": "get_application"
    }
  }
}
```

Use stable machine-readable `error.code` values. Keep `message` safe for users
and avoid credentials, tokens, or direct secret material in `details`.

## Source Guidance

Some legacy meta tools use `source` as a tool-specific string. For that reason,
the v1 envelope allows `source` to be a string, object, or null.

For integration-backed tools, prefer a structured provenance object:

```json
{
  "source": {
    "type": "argocd",
    "integration_id": "52b8046c-35f9-48ff-839b-d76d84092b8e",
    "integration_name": "Production Argo CD",
    "freshness": "live",
    "fetched_at": "2026-07-19T18:00:00Z"
  }
}
```

If the contract moves to v2, `source` should become consistently structured.

## Schema IDs

Schema IDs are derived from tool names:

- `argocd_get_last_operation` -> `argocd.get-last-operation`
- `grafana_metrics_query` -> `grafana.metrics-query`
- `k8s_list_pods` -> `kubernetes.list-pods`
- `slack_alerts_list` -> `slack.alerts-list`
- `knowledge_upsert` -> `knowledge.upsert`

## Schema Generation

Run:

```bash
uv run python scripts/generate_tool_schemas.py
```

Generated files are written to:

```text
schemas/tools/
```

The generated catalog contains:

- `tool-envelope.v1.schema.json`
- one `*.v1.schema.json` file per registered MCP tool

Each generated schema includes a canonical `$id`, for example:

```json
{
  "$id": "https://incidentflow.io/schemas/tools/argocd.get-last-operation.v1.schema.json"
}
```

## Validation Strategy

Runtime tests validate real tool responses from the FastMCP tool manager against
their generated Pydantic response models. Integration-specific payload fields are
currently allowed as extra top-level fields in v1 so existing clients keep
working. Individual tool payloads can be tightened gradually by replacing the
generic per-tool response model with a stricter Pydantic model.

## GitHub Actions Check

The repository includes a dedicated workflow:

```text
.github/workflows/tool-contracts.yml
```

The check runs on pull requests and pushes that touch tool contracts, schemas,
tool code, or related tests. It performs the full v1 contract gate:

1. Generate the schema catalog:

   ```bash
   uv run python scripts/generate_tool_schemas.py
   ```

2. Fail if generated schemas are stale or not committed:

   ```bash
   git diff --exit-code -- schemas/tools
   ```

3. Lint the contract code and tests:

   ```bash
   uv run ruff check ...
   ```

4. Run the runtime response contract tests:

   ```bash
   uv run pytest tests/test_tool_response_contracts.py ...
   ```

This means a pull request fails when a tool is added or renamed but its generated
schema file is not updated, or when a real FastMCP response no longer validates
against the global `schemaVersion` / `schemaId` contract.

## v2 Direction

For a future stricter contract:

- make `ok` required for every tool response
- standardize `status` as an enum
- require the preferred structured `error` object
- require structured `source` for integration-backed tools
- keep per-tool payload models strict once the migration risk is low
