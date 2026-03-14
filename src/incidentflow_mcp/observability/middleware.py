"""HTTP middleware for production-safe MCP observability."""

from __future__ import annotations

import logging
from time import monotonic, perf_counter
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.config import Settings
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
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = max(0.0, perf_counter() - started)
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
