"""Prometheus metrics and low-cardinality helpers for HTTP + MCP observability."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST

_HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

_SESSION_DURATION_BUCKETS = (
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
    3600.0,
    14400.0,
)

_MCP_METHOD_TO_REQUEST_TYPE = {
    "initialize": "InitializeRequest",
    "ping": "PingRequest",
    "tools/list": "ListToolsRequest",
    "tools/call": "CallToolRequest",
    "prompts/list": "ListPromptsRequest",
    "prompts/get": "GetPromptRequest",
    "resources/list": "ListResourcesRequest",
    "resources/read": "ReadResourceRequest",
    "completion/complete": "CompleteRequest",
}

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_KNOWN_ROUTES = frozenset({
    "/mcp",
    "/healthz",
    "/readyz",
    "/metrics",
    "/install.sh",
})

http_requests_total = Counter(
    "http_requests_total",
    "Total number of HTTP requests by normalized route/method/status.",
    ("method", "route", "status_code", "traffic"),
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds by normalized route/method/status.",
    ("method", "route", "status_code", "traffic"),
    buckets=_HTTP_DURATION_BUCKETS,
)
http_requests_in_flight = Gauge(
    "http_requests_in_flight",
    "Number of in-flight HTTP requests currently being processed.",
    ("method", "route", "traffic"),
)
http_request_errors_total = Counter(
    "http_request_errors_total",
    "Total number of HTTP error responses (4xx/5xx).",
    ("method", "route", "status_code", "traffic"),
)

mcp_sessions_active = Gauge(
    "mcp_sessions_active",
    "Current number of active MCP sessions (best-effort inferred).",
)
mcp_sessions_started_total = Counter(
    "mcp_sessions_started_total",
    "Total number of MCP session start events by reason.",
    ("reason",),
)
mcp_sessions_ended_total = Counter(
    "mcp_sessions_ended_total",
    "Total number of MCP sessions observed as ended.",
    ("reason",),
)
mcp_sessions_terminated_total = Counter(
    "mcp_sessions_terminated_total",
    "Deprecated alias of mcp_sessions_ended_total.",
    ("reason",),
)
mcp_session_duration_seconds = Histogram(
    "mcp_session_duration_seconds",
    "Observed MCP session duration in seconds.",
    ("reason",),
    buckets=_SESSION_DURATION_BUCKETS,
)
mcp_connections_active = Gauge(
    "mcp_connections_active",
    "Current in-flight MCP HTTP activity (not session lifecycle).",
    ("namespace", "pod", "traffic_type", "session_mode"),
)

mcp_request_type_total = Counter(
    "mcp_request_type_total",
    "Total number of MCP requests by MCP request type and status code.",
    ("request_type", "status_code"),
)
mcp_request_type_duration_seconds = Histogram(
    "mcp_request_type_duration_seconds",
    "MCP request latency in seconds by request type and status code.",
    ("request_type", "status_code"),
    buckets=_HTTP_DURATION_BUCKETS,
)
mcp_tool_requests_total = Counter(
    "mcp_tool_requests_total",
    "Total MCP tool requests by tool/method/status/outcome.",
    (
        "namespace",
        "pod",
        "tool",
        "method",
        "status_code",
        "status_class",
        "outcome",
        "traffic_type",
        "session_mode",
    ),
)
mcp_tool_request_duration_seconds = Histogram(
    "mcp_tool_request_duration_seconds",
    "MCP tool request latency in seconds by tool/method/outcome.",
    ("namespace", "pod", "tool", "method", "outcome", "traffic_type"),
    buckets=_HTTP_DURATION_BUCKETS,
)
mcp_tool_requests_in_flight = Gauge(
    "mcp_tool_requests_in_flight",
    "Current in-flight MCP tool requests.",
    ("namespace", "pod", "tool", "traffic_type"),
)
mcp_tool_errors_total = Counter(
    "mcp_tool_errors_total",
    "Total MCP tool request errors.",
    ("namespace", "pod", "tool", "method", "status_code", "status_class", "traffic_type"),
)
mcp_platform_api_jobs_errors_total = Counter(
    "mcp_platform_api_jobs_errors_total",
    "Total platform-api async jobs client errors by operation and status code.",
    ("namespace", "pod", "operation", "status_code", "error_type"),
)


def render_prometheus_metrics() -> bytes:
    return generate_latest()


def normalize_route(path: str) -> str:
    if path in _KNOWN_ROUTES:
        return path
    return "other"


def classify_traffic(route: str) -> str:
    if route in ("/healthz", "/readyz"):
        return "probe"
    if route == "/mcp":
        return "business"
    return "other"


def classify_status(status_code: int) -> str:
    if status_code >= 500:
        return "5xx"
    if status_code >= 400:
        return "4xx"
    if status_code >= 300:
        return "3xx"
    if status_code >= 200:
        return "2xx"
    return "1xx"


def classify_outcome(status_code: int) -> str:
    return "success" if status_code < 400 else "error"


def status_class_from_code(status_code: int) -> str:
    return classify_status(status_code)


def detect_mcp_request_type(payload: Any) -> str:
    """Best-effort request-type extraction with bounded cardinality."""
    if isinstance(payload, list):
        if not payload:
            return "unknown"
        parsed = {detect_mcp_request_type(item) for item in payload if isinstance(item, dict)}
        parsed.discard("unknown")
        if not parsed:
            return "unknown"
        if len(parsed) == 1:
            return next(iter(parsed))
        return "BatchRequest"

    if not isinstance(payload, dict):
        return "unknown"

    method = payload.get("method")
    if isinstance(method, str):
        return _MCP_METHOD_TO_REQUEST_TYPE.get(method, "unknown")

    request_type = payload.get("type")
    if isinstance(request_type, str) and request_type.endswith("Request"):
        return request_type

    return "unknown"


def extract_tool_name(payload: Any) -> str:
    """Extract normalized tool name from tools/call payload."""
    if isinstance(payload, list):
        # Mixed batch calls are represented as unknown for low cardinality.
        return "unknown"

    if not isinstance(payload, dict):
        return "unknown"

    method = payload.get("method")
    if method != "tools/call":
        return "unknown"

    params = payload.get("params")
    if not isinstance(params, dict):
        return "unknown"

    raw_name = params.get("name")
    if not isinstance(raw_name, str):
        return "unknown"

    normalized = raw_name.strip()
    if not normalized:
        return "unknown"
    if not _TOOL_NAME_RE.match(normalized):
        return "unknown"
    return normalized


def pod_label_values() -> tuple[str, str]:
    namespace = os.getenv("POD_NAMESPACE", "unknown")
    pod = os.getenv("POD_NAME") or os.getenv("HOSTNAME") or "unknown"
    return namespace, pod


@dataclass
class _SessionState:
    started_at: float
    last_seen_at: float


class SessionTracker:
    """Tracks active sessions in-memory for per-pod metrics."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, _SessionState] = {}

    def touch(self, session_id: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else monotonic()
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                self._sessions[session_id] = _SessionState(started_at=ts, last_seen_at=ts)
                mcp_sessions_started_total.labels(reason="header").inc()
                mcp_sessions_active.set(len(self._sessions))
                return True

            state.last_seen_at = ts
            return False

    def terminate(self, session_id: str, *, reason: str, now: float | None = None) -> bool:
        ts = now if now is not None else monotonic()
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is None:
                return False

            duration = max(0.0, ts - state.started_at)
            mcp_sessions_ended_total.labels(reason=reason).inc()
            mcp_sessions_terminated_total.labels(reason=reason).inc()
            mcp_session_duration_seconds.labels(reason=reason).observe(duration)
            mcp_sessions_active.set(len(self._sessions))
            return True

    def reap_idle(self, *, idle_timeout_seconds: int, now: float | None = None) -> int:
        if idle_timeout_seconds <= 0:
            return 0

        ts = now if now is not None else monotonic()
        expired: list[tuple[str, float]] = []

        with self._lock:
            for session_id, state in self._sessions.items():
                if (ts - state.last_seen_at) > float(idle_timeout_seconds):
                    expired.append((session_id, state.started_at))

            for session_id, _ in expired:
                self._sessions.pop(session_id, None)

            if expired:
                mcp_sessions_active.set(len(self._sessions))

        for _, started_at in expired:
            duration = max(0.0, ts - started_at)
            mcp_sessions_ended_total.labels(reason="idle_timeout").inc()
            mcp_sessions_terminated_total.labels(reason="idle_timeout").inc()
            mcp_session_duration_seconds.labels(reason="idle_timeout").observe(duration)

        return len(expired)
