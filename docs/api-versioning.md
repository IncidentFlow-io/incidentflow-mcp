# IncidentFlow API & Schema Versioning

This document defines the versioning model shared by the IncidentFlow MCP server
and the platform-api HTTP surface, the compatibility and deprecation rules, and
the catalog of schema IDs.

## 1. The three version axes

IncidentFlow separates three independent versions. They must never be conflated.

| Version | Example | Meaning | Where it appears |
|---|---|---|---|
| `service_version` | `1.0.54` | Version of a specific application build. | `mcp_version.data.service_version`, logs, OTEL `service.version`, image metadata, FastAPI `version`, ops-health. |
| `api_version` | `v1` | Version of the HTTP/MCP API surface. | Every response envelope, `mcp_version`, capabilities, `X-IncidentFlow-API-Version` (HTTP). |
| `schema_version` | `1.0` | Version of the response **structure** (the envelope + data schemas). | Every response envelope, `X-IncidentFlow-Schema-Version` (HTTP). |

The MCP **protocol version** (negotiated by the MCP framework) is a separate
concept and is **not** mixed with any IncidentFlow version.

### Single source of truth for `service_version`

`service_version` is resolved in exactly one place —
`incidentflow_mcp.version.resolve_service_version(settings)` — with this precedence:

1. build-time metadata (`MCP_BUILD_VERSION` / `MCP_BUILD_TAG`, normalized);
2. installed package version (`importlib.metadata.version("incidentflow-mcp")`, i.e. `pyproject.toml`);
3. configured `mcp_server_version` fallback.

Logs, the OTEL resource, the FastAPI app version, the ops-health endpoint and the
`mcp_version` tool all read from this function. The legacy `service_version=0.0.0`
telemetry default is no longer hardcoded separately.

## 2. Change classification

| Change | Version bump |
|---|---|
| Add an **optional** field to a `data` schema | `schema_version` **minor** (`1.0` → `1.1`) |
| **Remove or rename** a field | `schema_version` **major** (`1.0` → `2.0`) |
| Change the **semantics** of an API route/behavior | new `api_version` **major** (`v1` → `v2`) |
| Internal fix with no contract change | only `service_version` changes |

## 3. Compatibility rules

- New optional fields are additive; clients must ignore unknown fields.
- Field removal/rename requires a new `schema_version` major and a deprecation
  window (below).
- A new `api_version` is served **alongside** the old one; the old one is not
  removed until its sunset date.
- Order of operations for any breaking change: **(1)** add the adapter /
  compatibility layer, **(2)** migrate consumers, **(3)** only then remove old
  fields or routes.

## 4. Deprecation policy

- Deprecations are announced in `mcp_version.deprecated_api_versions` (MCP) and via
  HTTP headers on legacy routes:

  ```http
  Deprecation: true
  Sunset: <RFC 1123 date>
  Link: <canonical-route>; rel="successor-version"
  ```

- Minimum window between announcing a sunset and removal: **one minor release
  train** (and never less than 30 days) for schema fields; a full major cycle for
  API routes.
- A field slated for removal is first marked in `meta.warnings[]` on responses that
  still return it.

## 5. Schema-ID catalog

Schema IDs are stable, kebab-cased, and namespaced under `incidentflow.`.

| Kind | Pattern | Example |
|---|---|---|
| Response envelope (per tool) | `incidentflow.<tool>.response` | `incidentflow.external-status-check.response` |
| Request (per tool) | `incidentflow.<tool>.request` | `incidentflow.external-status-check.request` |
| Common error | `incidentflow.common.error` | `incidentflow.common.error` |
| Common (generic) envelope | `incidentflow.common.envelope` | `incidentflow.common.envelope` |

The five tools covered by the first rollout:

| Tool | Response schema id |
|---|---|
| `mcp_version` | `incidentflow.mcp-version.response` |
| `external_status_check` | `incidentflow.external-status-check.response` |
| `k8s_agent_status` | `incidentflow.k8s-agent-status.response` |
| `argocd_connection_health` | `incidentflow.argocd-connection-health.response` |
| `public_knowledge_search` | `incidentflow.public-knowledge-search.response` |

`incidentflow_capabilities` reports, per tool: `api_version`, `schema_version`,
`input_schema_id`, `output_schema_id`, `error_schema_id`.

Generated JSON Schema files live in `schemas/tools/` (Draft 2020-12, inline — no
external `$ref`, so a client that cannot resolve remote references still gets a
complete schema).

## 6. Before / after examples

### `mcp_version` — before

```json
{
  "service": "incidentflow-mcp",
  "version": "1.0.54",
  "tag": "v1.0.54",
  "environment": "dev",
  "tools": { "registered": 46, "operational": 42, "meta": 4 }
}
```

### `mcp_version` — after

```json
{
  "api_version": "v1",
  "schema_version": "1.0",
  "schema_id": "incidentflow.mcp-version.response",
  "status": "success",
  "request_id": "req_9f2c...",
  "data": {
    "service": "incidentflow-mcp",
    "service_version": "1.0.54",
    "api_version": "v1",
    "contract_version": "1.0",
    "supported_api_versions": ["v1"],
    "supported_schema_versions": ["1.0"],
    "deprecated_api_versions": [],
    "environment": "dev"
  },
  "error": null,
  "meta": { "generated_at": "2026-08-07T09:29:30Z", "truncated": false, "warnings": [] }
}
```

### `k8s_agent_status` — before (flat, ambiguous booleans)

```json
{ "status": "connected", "agent_online": true, "cluster_id": "c_1", "checked_at": "..." }
```

### `k8s_agent_status` — after (enveloped, normalized `healthy`)

```json
{
  "api_version": "v1", "schema_version": "1.0",
  "schema_id": "incidentflow.k8s-agent-status.response",
  "status": "success", "request_id": "req_...",
  "data": { "status": "connected", "healthy": true, "cluster_id": "c_1", "checked_at": "..." },
  "error": null,
  "meta": { "generated_at": "...", "truncated": false, "warnings": [] }
}
```

### Error — before vs after

```json
// before: {"ok": false, "status": "failed", "error": {"code": "HTTP_400", ...}}
// after:
{
  "status": "error",
  "data": null,
  "error": { "code": "INVALID_ARGUMENT", "message": "Invalid job_id format",
             "retryable": false, "details": { "field": "job_id", "expected": "uuid" } }
}
```

---

## 7. HTTP API canonical prefix & migration map (deferred phase)

> This is the **planned** platform-api HTTP workstream. It is **not** implemented in
> the MCP Phase 1 change set. It is recorded here so it can be executed safely and
> without touching production routing prematurely.

Target canonical public prefix: **`/v1/*`** on `api.incidentflow.io`.

Reality today (verified against `incidentflow-platform/services/platform-api/src/platform_api/api/router.py`):
almost everything is mounted under `/api/v1/*`; only `argocd_integrations` is also
mounted under a bare `/v1/...`. No consumer uses bare `/v1` except that lone mount.

### Consumers

| Consumer | Prefix(es) used today |
|---|---|
| BFF (`incidentflow-app`) | `/api/v1/*` (core) + `/api/integrations/*` (no `v1`) + `/oauth/*` |
| MCP (`incidentflow-mcp`) | `/api/v1/*` (Bearer) + `/internal/*` (internal key) |
| k8s-agent | `/api/v1/agents/register` only |

### Migration map

| Canonical route | Legacy alias(es) | Caller(s) | Deprecation date | Safe migration order |
|---|---|---|---|---|
| `/v1/*` | `/api/v1/*` | BFF, MCP, k8s-agent | TBD (announce with `v1` GA on `api.incidentflow.io`) | 1) add `/v1/*` aliases in `router.py`; 2) add `Deprecation`/`Sunset`/`Link` headers on `/api/v1/*`; 3) migrate BFF → MCP → k8s-agent; 4) remove `/api/v1/*` after sunset |
| `/v1/integrations/*` | `/api/v1/integrations/*`, `/api/integrations/*` | BFF, MCP | TBD | consolidate the 2–3 integration mounts onto `/v1/...`; keep `/api/integrations/*` until BFF migrates |
| (remove) bare `/v1/integrations/argocd` | — | none | immediate-safe once `/v1/*` canonical exists | fold into the canonical `/v1/*` mount |

### HTTP response headers (planned)

On every versioned response:

```http
X-IncidentFlow-API-Version: v1
X-IncidentFlow-Schema-Version: 1.0
X-Request-ID: <request-id>
```

The JSON body remains the **primary** source of contract metadata; headers are a
convenience mirror. `X-Request-ID` already exists end-to-end via
`RequestIDMiddleware`.

### Istio / infrastructure

`api.incidentflow.io` is not yet configured (no VirtualService). Adding the `/v1`
canonical host requires (a) a VirtualService for `api.incidentflow.io/v1/*` and
(b) extending `denyPublicInternal.hosts` to include `api.incidentflow.io` so
`/internal/*` stays closed to the internet. **No infrastructure or production
routing changes are made in Phase 1.**
