# MCP Observability (Prometheus + Grafana)

## Architecture Summary

- `MCPObservabilityMiddleware` instruments normalized HTTP routes with low-cardinality labels.
- `/healthz` and `/readyz` are tagged as `traffic="probe"`, `/mcp` is `traffic="business"`.
- `/metrics` is excluded from HTTP request metrics.
- MCP request type is parsed from JSON-RPC `method` safely (`unknown` fallback).
- Sessions are tracked best-effort via `mcp-session-id` header and idle-timeout eviction.

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

- Session starts per minute:
```promql
sum(rate(mcp_sessions_started_total[5m])) * 60
```

- Session terminations per minute:
```promql
sum(rate(mcp_sessions_terminated_total[5m])) * 60
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
    `mcp_sessions_terminated_total`, `mcp_request_type_total`
  - Reason: monotonically increasing event counts suited for rates/increases.

- Gauge:
  - `http_requests_in_flight`, `mcp_sessions_active`
  - Reason: instantaneous values that can go up and down.

- Histogram:
  - `http_request_duration_seconds`, `mcp_session_duration_seconds`,
    `mcp_request_type_duration_seconds`
  - Reason: supports p50/p95/p99 and latency distribution analysis in PromQL.

## Structured Logging Recommendations

- Log one line per request with:
  - `request_id`, `method`, `route`, `traffic`, `status_code`, `duration_ms`, `request_type`.
- Keep client IP out of Prometheus labels; include in logs only when needed for debugging/security policy.
- Avoid putting request IDs, raw URLs, tool args, or user identifiers into metric labels.

## Optional Tracing (OpenTelemetry)

- Add ASGI/FastAPI OTEL instrumentation and export traces to Tempo/Jaeger/OTLP collector.
- Attach `request_id` as span attribute and propagate `traceparent` headers.
- Keep Prometheus metrics even with tracing; metrics + traces are complementary.
