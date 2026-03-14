# MCP Observability (Prometheus + Grafana)

## Architecture Summary

- `MCPObservabilityMiddleware` instruments normalized HTTP routes with low-cardinality labels.
- `/healthz` and `/readyz` are tagged as `traffic="probe"`, `/mcp` as `traffic="business"`.
- `/metrics` is excluded from HTTP request metrics.
- MCP request type and tool name are parsed safely from JSON-RPC payload.
- Tool-level metrics are emitted for `CallToolRequest` (`tool="unknown"` fallback).
- `mcp_sessions_active` tracks lifecycle sessions only when `mcp-session-id` exists.
- `mcp_connections_active` tracks real-time MCP request activity even when session headers are absent.

## PromQL Panel Queries

- RPS (`/mcp`):
```promql
sum(rate(http_requests_total{route="/mcp",traffic="business"}[5m]))
```

- p95 latency (`/mcp`):
```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(http_request_duration_seconds_bucket{route="/mcp",traffic="business"}[5m]))
)
```

- p50 latency (`/mcp`):
```promql
histogram_quantile(
  0.50,
  sum by (le) (rate(http_request_duration_seconds_bucket{route="/mcp",traffic="business"}[5m]))
)
```

- p99 latency (`/mcp`):
```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket{route="/mcp",traffic="business"}[5m]))
)
```

- Error rate (`/mcp` 4xx+5xx ratio):
```promql
sum(rate(http_request_errors_total{route="/mcp",traffic="business"}[5m]))
/
clamp_min(sum(rate(http_requests_total{route="/mcp",traffic="business"}[5m])), 0.001)
```

- Status code distribution:
```promql
sum by (status_code) (rate(http_requests_total{route="/mcp",traffic="business"}[5m]))
```

- Active sessions:
```promql
sum(mcp_sessions_active)
```

- Active MCP connections/activity:
```promql
sum(mcp_connections_active{traffic_type="business"})
```

- Session starts per minute:
```promql
sum(rate(mcp_sessions_started_total[5m])) * 60
```

- Session starts by reason:
```promql
sum by (reason) (rate(mcp_sessions_started_total[5m]))
```

- Session terminations per minute:
```promql
sum(rate(mcp_sessions_ended_total[5m])) * 60
```

- Session duration p95:
```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(mcp_session_duration_seconds_bucket[15m]))
)
```

- Top MCP request types:
```promql
topk(10, sum by (request_type) (rate(mcp_request_type_total[5m])))
```

- MCP request-type p95 latency:
```promql
histogram_quantile(
  0.95,
  sum by (le, request_type) (rate(mcp_request_type_duration_seconds_bucket[5m]))
)
```

- Per-pod p95 latency (`/mcp`):
```promql
histogram_quantile(
  0.95,
  sum by (le, pod) (rate(http_request_duration_seconds_bucket{route="/mcp",traffic="business"}[5m]))
)
```

- Probe traffic volume:
```promql
sum(rate(http_requests_total{traffic="probe"}[5m]))
```

- Top tools by RPS:
```promql
topk(10, sum by (tool) (rate(mcp_tool_requests_total{traffic_type="business"}[5m])))
```

- Tool p95 latency:
```promql
histogram_quantile(
  0.95,
  sum by (le, tool) (rate(mcp_tool_request_duration_seconds_bucket{traffic_type="business"}[5m]))
)
```

- Tool error rate:
```promql
sum by (tool) (rate(mcp_tool_requests_total{traffic_type="business",outcome="error"}[5m]))
/
clamp_min(sum by (tool) (rate(mcp_tool_requests_total{traffic_type="business"}[5m])), 0.001)
```

- Unknown tool ratio:
```promql
sum(rate(mcp_tool_requests_total{tool="unknown",traffic_type="business"}[5m]))
/
clamp_min(sum(rate(mcp_tool_requests_total{traffic_type="business"}[5m])), 0.001)
```

## Alerting Rules

See: `k8s/monitoring/prometheusrule.yaml`

- `IncidentflowMCPP95LatencyHigh`
- `IncidentflowMCP5xxSpike`
- `IncidentflowMCPNoTraffic`
- `IncidentflowMCPTooManyActiveSessions`
- `IncidentflowMCPAbnormal202Rate`

## Metric Type Rationale

- Counter:
  - `http_requests_total`, `http_request_errors_total`, `mcp_sessions_started_total`,
    `mcp_sessions_ended_total`, `mcp_request_type_total`, `mcp_tool_requests_total`,
    `mcp_tool_errors_total`
  - Reason: monotonically increasing event counts suited for rates/increases.

- Gauge:
  - `http_requests_in_flight`, `mcp_sessions_active`, `mcp_connections_active`,
    `mcp_tool_requests_in_flight`
  - Reason: instantaneous values that can go up and down.

- Histogram:
  - `http_request_duration_seconds`, `mcp_session_duration_seconds`,
    `mcp_request_type_duration_seconds`, `mcp_tool_request_duration_seconds`
  - Reason: supports p50/p95/p99 and latency distribution analysis in PromQL.

## Structured Logging Recommendations

- Log one line per request with:
  - `request_id`, `method`, `route`, `traffic`, `status_code`, `duration_ms`,
    `request_type`, `tool`, `session_mode`.
- Keep client IP out of Prometheus labels; include in logs only when needed for debugging/security policy.
- Avoid putting request IDs, raw URLs, tool args, or user identifiers into metric labels.

## Optional Tracing (OpenTelemetry)

- Add ASGI/FastAPI OTEL instrumentation and export traces to Tempo/Jaeger/OTLP collector.
- Attach `request_id` as span attribute and propagate `traceparent` headers.
- Keep Prometheus metrics even with tracing; metrics + traces are complementary.

## Session vs Connection Semantics

- `mcp_sessions_active`:
  - Tracks explicit lifecycle sessions keyed by `mcp-session-id`.
  - If no session header exists, this may remain `0` even with heavy traffic.

- `mcp_connections_active`:
  - Tracks in-flight MCP HTTP activity (operational proxy for real load).
  - Works for both header-based and headerless clients via `session_mode` label.

- Fallback logic:
  - Headerless `POST /mcp` increments `mcp_sessions_started_total{reason="inferred_request"}`.
  - This is an activity/start event only, not a full lifecycle reconstruction.
