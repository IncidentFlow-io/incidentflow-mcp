"""Metrics used by rate limiting and MCP tool execution guards."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Protocol


class CounterLike(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...


class _FallbackCounter:
    _values: defaultdict[str, float] = defaultdict(float)
    _lock = Lock()

    def __init__(self, name: str) -> None:
        self._name = name

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._values[self._name] += amount


try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

    def _counter(name: str, doc: str) -> CounterLike:
        return Counter(name, doc)

    def render_prometheus_metrics() -> bytes:
        return generate_latest()

    METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
except ImportError:

    def _counter(name: str, doc: str) -> CounterLike:
        del doc
        return _FallbackCounter(name)

    def render_prometheus_metrics() -> bytes:
        lines = [
            "# Metrics backend unavailable (prometheus_client not installed)",
            "# Install optional dependency: prometheus-client",
        ]
        return "\n".join(lines).encode("utf-8")

    METRICS_CONTENT_TYPE = "text/plain; charset=utf-8"


mcp_http_requests_total = _counter(
    "mcp_http_requests_total",
    "Total number of HTTP requests that reached rate-limited MCP/auth endpoints.",
)
mcp_http_rate_limited_total = _counter(
    "mcp_http_rate_limited_total",
    "Total number of HTTP requests rejected by transport-level rate limiting.",
)
mcp_tool_calls_total = _counter(
    "mcp_tool_calls_total",
    "Total number of MCP tools/call requests.",
)
mcp_tool_rate_limited_total = _counter(
    "mcp_tool_rate_limited_total",
    "Total number of MCP tool invocations rejected by rate limiting.",
)
mcp_tool_timeouts_total = _counter(
    "mcp_tool_timeouts_total",
    "Total number of MCP tool invocations that exceeded execution timeout.",
)
mcp_tool_concurrency_rejections_total = _counter(
    "mcp_tool_concurrency_rejections_total",
    "Total number of MCP tool invocations rejected due to concurrency limits.",
)
