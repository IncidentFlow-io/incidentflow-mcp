"""HTTP middleware for production-safe MCP observability."""

from __future__ import annotations

import logging
from time import monotonic, perf_counter

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.metrics import (
    SessionTracker,
    classify_status,
    classify_traffic,
    detect_mcp_request_type,
    http_request_duration_seconds,
    http_request_errors_total,
    http_requests_in_flight,
    http_requests_total,
    mcp_request_type_duration_seconds,
    mcp_request_type_total,
    normalize_route,
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method.upper()
        route = normalize_route(request.url.path)

        # Never instrument the scrape endpoint itself.
        if route == "/metrics":
            return await call_next(request)

        traffic = classify_traffic(route)
        request_type = "unknown"

        if route == "/mcp" and method == "POST":
            request_type = await _extract_request_type(request)

        http_requests_in_flight.labels(method=method, route=route, traffic=traffic).inc()
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

            http_requests_in_flight.labels(method=method, route=route, traffic=traffic).dec()
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
                self._update_session_metrics(
                    request=request,
                    response=response,
                    status_code=status_code,
                )

            request_id = getattr(request.state, "request_id", None)
            logger.info(
                "http_request method=%s route=%s traffic=%s status_code=%d "
                "duration_ms=%.2f request_id=%s request_type=%s",
                method,
                route,
                traffic,
                status_code,
                elapsed * 1000.0,
                request_id,
                request_type if route == "/mcp" else "-",
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


async def _extract_request_type(request: Request) -> str:
    try:
        payload = await request.json()
    except Exception:
        return "unknown"

    return detect_mcp_request_type(payload)
