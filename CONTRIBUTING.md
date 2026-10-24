# Contributing to IncidentFlow MCP

Thank you for contributing to IncidentFlow MCP. This guide covers the local
workflow, quality checks, and pull-request expectations for this repository.

For security vulnerabilities, do not open a public issue. Follow
[SECURITY.md](SECURITY.md).

## Prerequisites

- Git
- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose (optional, for container-based development)

## Set up the project

Fork the repository, then clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/incidentflow-mcp.git
cd incidentflow-mcp
```

Install the locked development dependencies and create local configuration:

```bash
uv sync --all-groups --all-extras
cp .env.example .env
```

Do not commit `.env`, tokens, workspace IDs, or other credentials. The example
file documents the optional integrations and local authentication settings.

Start the server with auto-reload:

```bash
uv run incidentflow-mcp serve --reload
```

The default local endpoint is `http://localhost:8000/mcp`. Create a local
bearer token when you need to exercise authenticated requests:

```bash
uv run incidentflow-mcp token create --name local-dev
```

## Repository layout

```text
src/incidentflow_mcp/  Application, authentication, MCP tools, and integrations
tests/                 Unit, integration, and contract tests
schemas/tools/         Generated JSON Schemas for tool response contracts
docs/                  Architecture and operational documentation
openapi/               Generated HTTP API contract
scripts/               Generation and validation helpers
docker/ and k8s/       Local dependency and deployment assets
```

## Make a change

1. Create a focused branch from an up-to-date default branch.

   ```bash
   git switch -c feature/short-description
   ```

2. Keep changes scoped and add or update tests for behavioural changes.

3. Update user-facing documentation when the public API, configuration, tool
   catalogue, or operational behaviour changes.

4. Do not add credentials, customer data, captured logs, or generated local
   caches to the repository.

## Quality checks

Run these before opening a pull request:

```bash
make lint
make test
```

Run the relevant focused test while iterating:

```bash
uv run --group dev pytest tests/test_auth.py
```

Changes to tool inputs, outputs, or the canonical response envelope also require
the contract checks:

```bash
uv run python scripts/generate_tool_schemas.py
uv run python scripts/contract_check.py
git diff --exit-code -- schemas/tools
```

Commit regenerated schemas with the corresponding implementation change.

For HTTP API or documentation changes, validate the OpenAPI and Fern setup:

```bash
make openapi-generate
make openapi-validate
make fern-check
```

## Adding or changing an MCP tool

The tool registry is the source of truth for public tool metadata. A typical
tool change involves:

1. Add or update the `ToolSpec` in
   `src/incidentflow_mcp/tools/registry.py`.
2. Implement and register the handler in
   `src/incidentflow_mcp/mcp/server.py` or the appropriate registration module.
3. Add a precise output model in
   `src/incidentflow_mcp/tools/output_models.py`.
4. Add or update tests, then regenerate
   `schemas/tools/` with `scripts/generate_tool_schemas.py`.
5. Update the ChatGPT Apps submission metadata in
   `apps/chatgpt/submission.json` when its published tool metadata changes.

Tools must declare accurate safety annotations. Infrastructure and integration
tools are read-only by default; any write behaviour requires an explicit review
of authorization, workspace scoping, auditability, and user impact.

## Pull requests

A pull request should explain:

- What changed and why.
- Any user-visible API, configuration, schema, or documentation change.
- The validation commands run and their outcome.
- Follow-up work or known limitations, if any.

Use a concise, imperative commit subject, such as
`Add Kubernetes rollout inspection`. Keep unrelated formatting or refactoring
in separate changes where practical.

Maintainers review for correctness, tests, security boundaries, documentation,
and compatibility. Address review feedback with follow-up commits; do not force
push over commits that reviewers may already be discussing unless asked.

## Reporting issues

For bugs and feature requests, open a GitHub issue with:

- A concise description of the problem or use case.
- Steps to reproduce and the expected result.
- Actual behaviour, including a redacted error or request ID when available.
- Version, environment, and relevant integration details.

Never include access tokens, private URLs, customer data, or unredacted logs in
a public issue.
