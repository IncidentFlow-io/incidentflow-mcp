"""HTTP middleware for production-safe MCP observability."""

from __future__ import annotations

import logging
from time import monotonic, perf_counter
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer
from incidentflow_mcp.observability.metrics import (
    SessionTracker,
    classify_outcome,
    classify_status,
    classify_traffic,
    detect_mcp_request_type,
    extract_tool_name,
    http_request_duration_seconds,
    http_request_errors_total,
    http_requests_in_flight,
    http_requests_total,
    mcp_connections_active,
    mcp_request_type_duration_seconds,
    mcp_request_type_total,
    mcp_session_duration_seconds,
    mcp_sessions_ended_total,
    mcp_sessions_started_total,
    mcp_tool_errors_total,
    mcp_tool_request_duration_seconds,
    mcp_tool_requests_in_flight,
    mcp_tool_requests_total,
    normalize_route,
    pod_label_values,
    status_class_from_code,
)

logger = logging.getLogger(__name__)

_SESSION_ID_HEADER = "mcp-session-id"
_SESSION_END_HEADERS: frozenset[str] = frozenset({"1", "true", "yes"})


class MCPObservabilityMiddleware(BaseHTTPMiddleware):
    """Captures low-cardinality Prometheus metrics and structured request logs."""

    def __init__(self, app, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._session_tracker = SessionTracker()
        self._session_idle_timeout_seconds = settings.mcp_session_idle_timeout_seconds
        self._namespace, self._pod = pod_label_values()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method.upper()
        route = normalize_route(request.url.path)

        # Never instrument the scrape endpoint itself.
        if route == "/metrics":
            return await call_next(request)

        traffic = classify_traffic(route)
        request_type = "unknown"
        tool_name = "unknown"
        is_call_tool = False
        payload: Any = None
        session_mode = _detect_session_mode(request)

        if route == "/mcp" and method == "POST":
            payload = await _extract_payload(request)
            request_type = detect_mcp_request_type(payload)
            is_call_tool = request_type == "CallToolRequest"
            if is_call_tool:
                tool_name = extract_tool_name(payload)

        http_requests_in_flight.labels(method=method, route=route, traffic=traffic).inc()
        mcp_connections_active.labels(
            namespace=self._namespace,
            pod=self._pod,
            traffic_type=traffic,
            session_mode=session_mode,
        ).inc()
        if is_call_tool:
            mcp_tool_requests_in_flight.labels(
                namespace=self._namespace,
                pod=self._pod,
                tool=tool_name,
                traffic_type=traffic,
            ).inc()
        started = perf_counter()

        status_code = 500
        response: Response | None = None
        _otel_span = None
        _otel_token = None
        try:
            if is_call_tool:
                _otel_span, _otel_token = _start_tool_span(tool_name, request)
            response = await call_next(request)
            status_code = response.status_code
            if _otel_span is not None:
                try:
                    from opentelemetry.trace import StatusCode
                    if status_code >= 500:
                        _otel_span.set_status(StatusCode.ERROR)
                    else:
                        _otel_span.set_status(StatusCode.OK)
                except Exception:
                    pass
            return response
        except Exception as exc:
            if _otel_span is not None:
                try:
                    from opentelemetry.trace import StatusCode
                    _otel_span.record_exception(exc)
                    _otel_span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
            raise
        finally:
            elapsed = max(0.0, perf_counter() - started)
            if _otel_span is not None:
                try:
                    _otel_span.set_attribute("mcp.http.duration_ms", round(elapsed * 1000, 2))
                    _otel_span.end()
                except Exception:
                    pass
            if _otel_token is not None:
                try:
                    import opentelemetry.context as otel_ctx
                    otel_ctx.detach(_otel_token)
                except Exception:
                    pass
            status_code_str = str(status_code)
            status_class = status_class_from_code(status_code)
            outcome = classify_outcome(status_code)

            http_requests_in_flight.labels(method=method, route=route, traffic=traffic).dec()
            mcp_connections_active.labels(
                namespace=self._namespace,
                pod=self._pod,
                traffic_type=traffic,
                session_mode=session_mode,
            ).dec()
            http_requests_total.labels(
                method=method,
                route=route,
                status_code=status_code_str,
                traffic=traffic,
            ).inc()
            http_request_duration_seconds.labels(
                method=method,
                route=route,
                status_code=status_code_str,
                traffic=traffic,
            ).observe(elapsed)

            if classify_status(status_code) in {"4xx", "5xx"}:
                http_request_errors_total.labels(
                    method=method,
                    route=route,
                    status_code=status_code_str,
                    traffic=traffic,
                ).inc()

            if route == "/mcp":
                mcp_request_type_total.labels(
                    request_type=request_type,
                    status_code=status_code_str,
                ).inc()
                mcp_request_type_duration_seconds.labels(
                    request_type=request_type,
                    status_code=status_code_str,
                ).observe(elapsed)
                if is_call_tool:
                    mcp_tool_requests_total.labels(
                        namespace=self._namespace,
                        pod=self._pod,
                        tool=tool_name,
                        method=request_type,
                        status_code=status_code_str,
                        status_class=status_class,
                        outcome=outcome,
                        traffic_type=traffic,
                        session_mode=session_mode,
                    ).inc()
                    mcp_tool_request_duration_seconds.labels(
                        namespace=self._namespace,
                        pod=self._pod,
                        tool=tool_name,
                        method=request_type,
                        outcome=outcome,
                        traffic_type=traffic,
                    ).observe(elapsed)
                    if outcome == "error":
                        mcp_tool_errors_total.labels(
                            namespace=self._namespace,
                            pod=self._pod,
                            tool=tool_name,
                            method=request_type,
                            status_code=status_code_str,
                            status_class=status_class,
                            traffic_type=traffic,
                        ).inc()
                    mcp_tool_requests_in_flight.labels(
                        namespace=self._namespace,
                        pod=self._pod,
                        tool=tool_name,
                        traffic_type=traffic,
                    ).dec()
                self._update_session_metrics(
                    request=request,
                    response=response,
                    status_code=status_code,
                )

            request_id = getattr(request.state, "request_id", None)
            logger.info(
                "http_request method=%s route=%s traffic=%s status_code=%d "
                "duration_ms=%.2f request_id=%s request_type=%s tool=%s session_mode=%s",
                method,
                route,
                traffic,
                status_code,
                elapsed * 1000.0,
                request_id,
                request_type if route == "/mcp" else "-",
                tool_name if is_call_tool else "-",
                session_mode if route == "/mcp" else "-",
            )

    def _update_session_metrics(
        self,
        *,
        request: Request,
        response: Response | None,
        status_code: int,
    ) -> None:
        now = monotonic()
        self._session_tracker.reap_idle(
            idle_timeout_seconds=self._session_idle_timeout_seconds,
            now=now,
        )

        request_session_id = request.headers.get(_SESSION_ID_HEADER)
        response_session_id = response.headers.get(_SESSION_ID_HEADER) if response else None
        session_id = response_session_id or request_session_id

        if session_id:
            self._session_tracker.touch(session_id, now=now)
        elif request.url.path == "/mcp" and request.method.upper() == "POST":
            # Headerless MCP traffic still represents operational activity.
            mcp_sessions_started_total.labels(reason="inferred_request").inc()

        response_marked_ended = (
            response.headers.get("mcp-session-ended", "").strip().lower() in _SESSION_END_HEADERS
            if response
            else False
        )
        explicit_session_end = (
            request.headers.get("mcp-session-ended", "").strip().lower() in _SESSION_END_HEADERS
            or response_marked_ended
            or request.method.upper() == "DELETE"
        )
        terminal_status = status_code in {410}

        if session_id and (explicit_session_end or terminal_status):
            reason = "explicit_end" if explicit_session_end else "terminal_status"
            self._session_tracker.terminate(session_id, reason=reason, now=now)
        elif not session_id and terminal_status:
            mcp_sessions_ended_total.labels(reason="inferred_terminal_status").inc()
            mcp_session_duration_seconds.labels(reason="inferred_terminal_status").observe(0.0)


def _detect_session_mode(request: Request) -> str:
    if request.headers.get(_SESSION_ID_HEADER):
        return "header"
    if request.url.path == "/mcp":
        return "headerless"
    return "none"


async def _extract_payload(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


def _start_tool_span(
    tool_name: str,
    request: Request,
) -> tuple[Any, Any]:
    """Start an mcp.tool.execute span and attach it as the current span.

    Returns (span, context_token) so the caller can end the span and detach
    the context in the finally block.  Returns (None, None) if OTEL is absent.
    """
    try:
        import opentelemetry.context as otel_ctx
        from opentelemetry.trace import SpanKind

        tracer = get_tracer()
        span = tracer.start_span(
            "mcp.tool.execute",
            kind=SpanKind.SERVER,
        )

        # Attach span attributes
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("mcp.tool.name", tool_name)
        span.set_attribute("mcp.request.type", "CallToolRequest")
        span.set_attribute("mcp.transport", "http")

        # Tool category based on name prefix
        if tool_name.startswith("k8s_"):
            span.set_attribute("tool.category", "kubernetes")
        elif tool_name.startswith("memory_"):
            span.set_attribute("tool.category", "memory")
        elif tool_name.startswith("slack_"):
            span.set_attribute("tool.category", "slack")
        elif tool_name.startswith("ai_"):
            span.set_attribute("tool.category", "ai")
        else:
            span.set_attribute("tool.category", "general")

        # Best-effort: read workspace/request-id from auth/request state
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            span.set_attribute("request.id", str(request_id))

        # MCP session ID
        session_id = request.headers.get("mcp-session-id")
        if session_id:
            span.set_attribute("mcp.session.id", session_id)

        # Client user-agent (e.g. "Claude Desktop", "ChatGPT")
        user_agent = request.headers.get("user-agent", "")
        if user_agent:
            span.set_attribute("mcp.client.user_agent", user_agent[:128])

        # Workspace ID from auth context (set by auth middleware upstream)
        auth_ctx = getattr(request.state, "auth_context", None)
        if isinstance(auth_ctx, dict):
            workspace_id = auth_ctx.get("workspace_id")
            if workspace_id:
                span.set_attribute("workspace.id", str(workspace_id))
            user_id = auth_ctx.get("user_id") or auth_ctx.get("sub")
            if user_id:
                import hashlib
                span.set_attribute("user.id.hash", hashlib.sha256(str(user_id).encode()).hexdigest()[:16])

        # Attach the span as current context so child spans nest under it
        from opentelemetry import trace
        ctx = trace.set_span_in_context(span)
        token = otel_ctx.attach(ctx)
        return span, token
    except Exception:
        return None, None
