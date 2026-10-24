# IncidentFlow MCP

[![Tool Contracts](https://github.com/IncidentFlow-io/incidentflow-mcp/actions/workflows/tool-contracts.yml/badge.svg)](https://github.com/IncidentFlow-io/incidentflow-mcp/actions/workflows/tool-contracts.yml)

IncidentFlow MCP is an HTTP-based Model Context Protocol server for investigating
and operating incidents across Kubernetes, observability platforms, and approved
workspace integrations.

[Hosted MCP](https://mcp.incidentflow.io/mcp) · [Documentation](docs/) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## What it does

- Investigates Kubernetes workloads with read-only tools.
- Reads approved Grafana dashboards and executes guarded PromQL queries.
- Brings together incident, alert, Slack, deployment, and external-status context.
- Keeps access workspace-scoped, authenticated, and auditable.

## Quick start

### Hosted server

The hosted server is a Streamable HTTP MCP endpoint at
`https://mcp.incidentflow.io/mcp`. It uses OAuth 2.1 with PKCE; configure it in
your MCP client and complete its browser sign-in flow.

#### Claude Code

```bash
claude mcp add --transport http incidentflow https://mcp.incidentflow.io/mcp
claude mcp login incidentflow
```

#### Codex CLI

```bash
codex mcp add incidentflow \
  --url https://mcp.incidentflow.io/mcp \
  --oauth-resource https://mcp.incidentflow.io/mcp
codex mcp login incidentflow --scopes mcp:read,mcp:tools:run
```

#### VS Code

Add the server to `.vscode/mcp.json`:

```json
{
  "servers": {
    "incidentflow": {
      "type": "http",
      "url": "https://mcp.incidentflow.io/mcp"
    }
  }
}
```

See the client documentation for the sign-in command or UI. The optional
[VS Code installer](scripts/install-vscode.sh) updates this file interactively.

### Local development

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/IncidentFlow-io/incidentflow-mcp.git
cd incidentflow-mcp
uv sync --all-groups --all-extras
cp .env.example .env
uv run incidentflow-mcp serve --reload
```

The server listens on `http://localhost:8000` by default. Set a local bearer
token with `uv run incidentflow-mcp token create --name local-dev`, then add the
printed token to the MCP client's configuration. See [authentication](docs/architecture/authentication.md)
for the supported local and hosted flows.

To run with Docker instead:

```bash
cp .env.example .env
docker compose up --build
```

`docker-compose.yml` also includes optional local Redis and Grafana smoke-test
profiles. Their operator instructions live with the deployment assets rather
than in this overview.

## Available tools

The server groups operational tools by task, rather than requiring a client to
know every tool name up front.

| Category | Examples |
| --- | --- |
| Kubernetes | Cluster and workload health, pods, logs, events, rollout state, RBAC checks |
| Observability | Grafana dashboards, safe PromQL queries, dashboard health |
| Incident intelligence | Incident summaries, alert correlation, Slack context, external status |
| Integrations | Argo CD deployment state and workspace knowledge |
| Investigations | Create, continue, inspect, and recheck scoped incident investigations |

All infrastructure and integration tools are read-only. `knowledge_upsert` is
the only write-capable tool and is limited to workspace knowledge.

Inspect the exact tool inventory and JSON schemas for the installed version:

```bash
uv run incidentflow-mcp tools list --json-output
```

Clients can also call `incidentflow_capabilities` to discover the same catalog
in-band.

## Security model

- OAuth access tokens or platform-managed tokens authenticate every hosted request.
- Workspace identity and entitlement checks scope integration access.
- Kubernetes, Grafana, Argo CD, Slack, and external-status operations default to read-only.
- Platform allow-lists and guarded queries constrain downstream access.
- Request IDs, audit-friendly structured logs, metrics, and tracing support investigation.

Do not place tokens in query strings, commit `.env` files, or share local token
stores. For vulnerability reporting, read [SECURITY.md](SECURITY.md).

## Documentation

- [Authentication architecture](docs/architecture/authentication.md)
- [Deployment and local configuration](docker-compose.yml)
- [Observability](docs/observability.md)
- [Logging](docs/logging.md)
- [Tool response contracts](docs/tool-response-contracts.md)
- [API versioning](docs/api-versioning.md)
- [Integration-status tools and metrics](docs/integration-status-tools-and-metrics.md)
- [OpenAPI contract](openapi/openapi.yaml)

The `fern/` directory contains the internal documentation publishing setup. Run
`make docs-all` to generate and validate the OpenAPI contract and validate its
Fern configuration.

## Development

```bash
make test
make lint
make openapi-validate
```

Before opening a pull request, follow [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

```text
src/        MCP server implementation
tests/      Unit, integration, and contract tests
docs/       Architecture and operational documentation
openapi/    Generated HTTP API contract
docker/     Local dependency and smoke-test assets
k8s/        Kubernetes manifests and monitoring rules
scripts/    Development, validation, and client-install helpers
```

## License

This repository is licensed under the [MIT License](LICENSE). IncidentFlow Cloud
and hosted services are proprietary.
