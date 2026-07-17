# IncidentFlow MCP Logging

This document describes the logging work completed for `incidentflow-mcp`.
The goal is to keep local logs readable while producing clean, machine-readable
JSON logs for containers, Loki/Grafana, and production debugging.

## Run Modes

Use readable text logs for local development:

```bash
uv run incidentflow-mcp serve \
  --reload \
  --host 127.0.0.1 \
  --port 8001 \
  --log-format text
```

Use JSON logs for ingestion:

```bash
uv run incidentflow-mcp serve \
  --reload \
  --host 127.0.0.1 \
  --port 8001 \
  --log-format json
```

Reduce third-party startup/reload noise when needed:

```bash
uv run incidentflow-mcp serve \
  --reload \
  --host 127.0.0.1 \
  --port 8001 \
  --log-format json \
  --library-log-level error
```

Environment variable equivalent:

```bash
LOG_FORMAT=json LIBRARY_LOG_LEVEL=error \
  uv run incidentflow-mcp serve --host 127.0.0.1 --port 8001
```

## Reload Behavior

`uvicorn --reload` starts a parent reloader process and a child application
process. CLI flags are now propagated to the child through environment
variables so both processes use the same logging settings.

The CLI propagates:

- `HOST`
- `PORT`
- `LOG_LEVEL`
- `LOG_FORMAT`
- `LIBRARY_LOG_LEVEL`

`create_app()` configures logging immediately when the application is created,
before lifespan startup logs are emitted. This prevents mixed output where the
reloader prints JSON but the application child falls back to text logs.

## JSON Log Contract

Every JSON log line includes:

```json
{
  "timestamp": "2026-07-16T09:51:19.120Z",
  "level": "INFO",
  "service": "incidentflow-mcp",
  "service_version": "0.1.0",
  "environment": "dev",
  "logger": "incidentflow_mcp.observability.middleware",
  "event": "http_request_completed"
}
```

Rules:

- `event` contains the log message.
- `timestamp` is UTC with millisecond precision.
- `level` uses the standard Python logging level name.
- `service`, `service_version`, and `environment` are attached to every log line.
- `trace_id` and `span_id` are included only when an active OpenTelemetry span exists.
- Empty values are omitted.
- Uvicorn's `color_message` field is omitted because it contains terminal color formatting.
- Sensitive text such as `token=...`, `secret=...`, `password=...`, and Redis credentials is redacted.

## Text Log Contract

Text logs are optimized for local reading. They also omit empty trace fields.

Example:

```text
2026-07-16T09:51:19.120Z  INFO      incidentflow-mcp  service_version=0.1.0  environment=dev  incidentflow_mcp.observability.middleware  http_request_completed  http_method=GET  http_route=/mcp  http_status_code=200
```

When tracing is inactive, text logs do not print:

```text
trace_id= span_id=
```

## Request Log Contract

`MCPObservabilityMiddleware` emits one canonical request log per request.
`uvicorn.access` is disabled to avoid duplicate access logs.

Request log event:

```json
{
  "event": "http_request_completed",
  "http_method": "POST",
  "http_route": "/mcp",
  "traffic": "business",
  "http_status_code": 200,
  "http_status_class": "2xx",
  "outcome": "success",
  "http_duration_ms": 24.7,
  "request_id": "0b5d23a8-2ac4-44c3-93a8-114a13d45fb4",
  "mcp_request_type": "ListToolsRequest",
  "session_mode": "headerless"
}
```

Field notes:

- `http_route` is normalized and low-cardinality.
- `http_status_class` is derived from the status code, for example `2xx` or `5xx`.
- `outcome` is `success` for status codes below `400`, otherwise `error`.
- `http_duration_ms` is for human-readable logs. Prometheus histograms keep duration in seconds.
- `mcp_request_type` is present for `/mcp` requests.
- `tool_name` is present only for MCP `tools/call` requests where the tool name is known.
- Placeholder values such as `tool: "-"` are not emitted.

For a tool call, the request log may include:

```json
{
  "event": "http_request_completed",
  "mcp_request_type": "CallToolRequest",
  "tool_name": "incidentflow_auth_status"
}
```

## Auth Context Fields

Authenticated business request logs may include:

```json
{
  "workspace_id": "8a508d11-459a-4812-8af3-193395196049",
  "auth_method": "api_token"
}
```

Privacy and cardinality rules:

- Do not log access tokens.
- Do not log tool arguments by default.
- Do not log email as a persistent request field.
- Keep `workspace_id` and `auth_method` as JSON fields only.
- Do not promote `workspace_id`, `user_id`, `request_id`, or raw paths to Prometheus labels.

## Verification

Verify JSON logs are parseable:

```bash
uv run incidentflow-mcp serve \
  --host 127.0.0.1 \
  --port 8001 \
  --log-format json 2>&1 \
  | jq -R 'fromjson? | select(. != null)'
```

Verify a local MCP request log:

```bash
curl -sS http://127.0.0.1:8001/mcp \
  -H "Authorization: Bearer $INCIDENTFLOW_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | sed -n 's/^data: //p' \
  | jq .
```

Then inspect the server logs for `http_request_completed`.

## Tests

Logging behavior is covered by:

- `tests/test_logging_config.py`
  - JSON logs are parseable.
  - Empty `trace_id` and `span_id` are omitted.
  - `color_message` is omitted.
  - service metadata is present.
- `tests/test_cli_logging.py`
  - CLI logging settings are propagated to the reload child process.
- `tests/test_observability_metrics.py`
  - Request logs use structured `http_*` fields.
  - Missing tools do not emit placeholder `tool` or `tool_name` fields.

Run the focused checks:

```bash
uv run pytest tests/test_logging_config.py tests/test_cli_logging.py tests/test_observability_metrics.py -q
uv run ruff check \
  src/incidentflow_mcp/logging_config.py \
  src/incidentflow_mcp/cli/main.py \
  src/incidentflow_mcp/app.py \
  src/incidentflow_mcp/observability/middleware.py \
  tests/test_logging_config.py \
  tests/test_cli_logging.py \
  tests/test_observability_metrics.py
```
