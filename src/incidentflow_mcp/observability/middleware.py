"""HTTP middleware for production-safe MCP observability."""

from __future__ import annotations

import json
import logging
from time import monotonic, perf_counter
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.logging_config import compact_log_fields
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
    mcp_request_duration_seconds,
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
    tool_duration_seconds,
)
from incidentflow_mcp.observability.tool_events import (
    current_tool_event,
    reset_tool_event_context,
    start_tool_event_context,
)
from incidentflow_mcp.observability.tracing import get_tracer

logger = logging.getLogger(__name__)

_SESSION_ID_HEADER = "mcp-session-id"
_SESSION_END_HEADERS: frozenset[str] = frozenset({"1", "true", "yes"})


class MCPObservabilityMiddleware(BaseHTTPMiddleware):
    """Captures low-cardinality Prometheus metrics and structured request logs."""

    def __init__(self, app, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._session_tracker = SessionTracker()
        self._session_idle_timeout_seconds = settings.mcp_session_idle_timeout_seconds
        self._http_slow_request_threshold_ms = settings.http_slow_request_threshold_ms
        self._mcp_slow_tool_threshold_ms = settings.mcp_slow_tool_threshold_ms
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
        response_tool_event: dict[str, Any] | None = None
        _otel_span = None
        _otel_token = None
        _tool_event_token = start_tool_event_context() if is_call_tool else None
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
            if is_call_tool:
                response, response_tool_event = await _capture_mcp_tool_response_event(
                    response=response,
                    tool_name=tool_name,
                )
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
            # NOTE: otel_ctx.detach() is called AFTER the request log below so that
            # _TraceContextFilter can still read trace_id/span_id from the active context.
            status_code_str = str(status_code)
            status_class = status_class_from_code(status_code)
            outcome = classify_outcome(status_code)
            tool_event = response_tool_event or current_tool_event()
            tool_outcome = _tool_metric_outcome(http_outcome=outcome, tool_event=tool_event)

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
            mcp_request_duration_seconds.labels(
                endpoint=route,
                method=method,
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
                        outcome=tool_outcome,
                        traffic_type=traffic,
                        session_mode=session_mode,
                    ).inc()
                    mcp_tool_request_duration_seconds.labels(
                        namespace=self._namespace,
                        pod=self._pod,
                        tool=tool_name,
                        method=request_type,
                        outcome=tool_outcome,
                        traffic_type=traffic,
                    ).observe(elapsed)
                    if tool_name != "unknown":
                        tool_duration_seconds.labels(tool=tool_name, status=tool_outcome).observe(
                            elapsed
                        )
                    if tool_outcome == "error":
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
            duration_ms = round(elapsed * 1000.0, 2)
            workspace_id = _workspace_id_from_request(request)

            # Log while OTel context is still attached so _TraceContextFilter
            # can inject non-empty trace_id/span_id into this log record.
            self._log_http_request(
                method=method,
                route=route,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                request_id=request_id,
                workspace_id=workspace_id,
            )
            if is_call_tool:
                self._log_mcp_tool(
                    tool_name=tool_name,
                    request_type=request_type,
                    status_code=status_code,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    request_id=request_id,
                    workspace_id=workspace_id,
                    tool_event=tool_event,
                )
            if _tool_event_token is not None:
                reset_tool_event_context(_tool_event_token)
            if _otel_token is not None:
                try:
                    import opentelemetry.context as otel_ctx

                    otel_ctx.detach(_otel_token)
                except Exception:
                    pass

    def _log_http_request(
        self,
        *,
        method: str,
        route: str,
        path: str,
        status_code: int,
        duration_ms: float,
        request_id: object,
        workspace_id: str | None,
    ) -> None:
        base_fields = compact_log_fields(
            http_method=method,
            http_route=route,
            http_path=path if route == "unmatched" else None,
            http_status_code=status_code,
            http_duration_ms=duration_ms,
            request_id=request_id,
            workspace_id=workspace_id,
        )

        if status_code >= 500:
            logger.error(
                "http_request_failed",
                extra=compact_log_fields(
                    **base_fields,
                    error_code=f"http_{status_code}",
                    error_type="HTTPServerError",
                    log_message=f"HTTP request failed with status {status_code}",
                ),
            )
        elif status_code >= 400:
            logger.warning(
                "http_request_failed",
                extra=compact_log_fields(
                    **base_fields,
                    error_code=f"http_{status_code}",
                    error_type="HTTPClientError",
                    log_message=f"HTTP request failed with status {status_code}",
                ),
            )
        else:
            logger.info("http_request_completed", extra=base_fields)
            if duration_ms >= self._http_slow_request_threshold_ms:
                logger.warning(
                    "http_request_slow",
                    extra=compact_log_fields(
                        **base_fields,
                        slow_threshold_ms=self._http_slow_request_threshold_ms,
                    ),
                )

    def _log_mcp_tool(
        self,
        *,
        tool_name: str,
        request_type: str,
        status_code: int,
        outcome: str,
        duration_ms: float,
        request_id: object,
        workspace_id: str | None,
        tool_event: dict[str, Any] | None,
    ) -> None:
        if request_type != "CallToolRequest":
            return

        base_fields = compact_log_fields(
            tool_name=tool_name,
            integration=_integration_for_tool(tool_name),
            duration_ms=duration_ms,
            request_id=request_id,
            workspace_id=workspace_id,
        )
        if outcome == "error":
            logger.error(
                "mcp_tool_failed",
                extra=compact_log_fields(
                    **base_fields,
                    error_code=f"http_{status_code}",
                    error_type="MCPToolTransportError",
                    log_message=f"MCP tool request failed with HTTP status {status_code}",
                ),
            )
            return

        event_fields = (
            {key: value for key, value in tool_event.items() if key != "outcome"}
            if tool_event
            else {}
        )

        if tool_event and tool_event.get("outcome") == "rejected":
            logger.warning(
                "mcp_tool_rejected",
                extra=compact_log_fields(**(base_fields | event_fields)),
            )
            return

        if tool_event and tool_event.get("outcome") == "failed":
            logger.error(
                "mcp_tool_failed",
                extra=compact_log_fields(**(base_fields | event_fields)),
            )
            return

        logger.info("mcp_tool_succeeded", extra=base_fields)
        if duration_ms >= self._mcp_slow_tool_threshold_ms:
            logger.warning(
                "mcp_tool_slow",
                extra=compact_log_fields(
                    **base_fields,
                    slow_threshold_ms=self._mcp_slow_tool_threshold_ms,
                ),
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


def _workspace_id_from_request(request: Request) -> str | None:
    auth_context = getattr(request.state, "auth_context", None)
    if not isinstance(auth_context, dict):
        return None
    workspace_id = auth_context.get("workspace_id")
    return str(workspace_id) if workspace_id else None


def _integration_for_tool(tool_name: str) -> str | None:
    prefix, _, _ = tool_name.partition("_")
    return {
        "argocd": "argocd",
        "grafana": "grafana",
        "k8s": "kubernetes",
        "kubectl": "kubernetes",
        "memory": "memory",
        "slack": "slack",
    }.get(prefix)


def _integration_remediation(integration: object) -> str | None:
    if not integration:
        return None
    return f"connect_{integration!s}_integration"


def _tool_metric_outcome(
    *,
    http_outcome: str,
    tool_event: dict[str, Any] | None,
) -> str:
    if not tool_event:
        return http_outcome
    if tool_event.get("outcome") == "rejected":
        return "rejected"
    if tool_event.get("outcome") == "failed":
        return "error"
    return http_outcome


async def _extract_payload(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


async def _capture_mcp_tool_response_event(
    *,
    response: Response,
    tool_name: str,
) -> tuple[Response, dict[str, Any] | None]:
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type and "text/event-stream" not in content_type:
        return response, None

    body = await _response_body(response)
    rebuilt = Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=response.background,
    )
    return rebuilt, _mcp_tool_event_from_body(
        body=body,
        content_type=content_type,
        tool_name=tool_name,
    )


async def _response_body(response: Response) -> bytes:
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(str(chunk).encode())
    return b"".join(chunks)


def _mcp_tool_event_from_body(
    *,
    body: bytes,
    content_type: str,
    tool_name: str,
) -> dict[str, Any] | None:
    payload = _mcp_json_payload_from_body(body=body, content_type=content_type)
    if not isinstance(payload, dict):
        return None

    error = payload.get("error")
    if isinstance(error, dict):
        error_code = _optional_error_code(error.get("code"))
        return {
            "outcome": "rejected",
            "reason": _reason_from_mcp_error_code(error_code),
            "error_code": error_code,
            "error_type": "MCPJsonRpcError",
            "log_message": str(error["message"]) if error.get("message") else None,
            "retryable": _is_retryable_mcp_error(error_code),
        }

    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    structured = result.get("structuredContent")
    structured_payload = structured if isinstance(structured, dict) else {}
    if structured_payload.get("ok") is False:
        code = _structured_error_code(structured_payload)
        message = _structured_error_message(structured_payload)
        if code == "INTEGRATION_NOT_CONNECTED":
            integration = structured_payload.get("integration") or _integration_for_tool(tool_name)
            return {
                "outcome": "rejected",
                "reason": "integration_missing",
                "integration": integration,
                "error_code": code,
                "log_message": message,
                "retryable": False,
                "remediation": _integration_remediation(integration),
            }
        return {
            "outcome": "failed",
            "error_code": code or "mcp_tool_error",
            "error_type": "MCPToolStructuredError",
            "log_message": message,
            "retryable": False,
        }

    if result.get("isError") is True:
        return {
            "outcome": "failed",
            "error_code": "mcp_tool_error",
            "error_type": "MCPToolStructuredError",
            "log_message": _text_content_message(result),
            "retryable": False,
        }

    return None


def _mcp_json_payload_from_body(*, body: bytes, content_type: str) -> Any:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "text/event-stream" in content_type:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        text = "\n".join(data_lines).strip()
        if not text:
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _structured_error_code(payload: dict[str, Any]) -> str | None:
    code = payload.get("code")
    if code:
        return str(code)
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return None


def _structured_error_message(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    if message:
        return str(message)
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return None


def _optional_error_code(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _reason_from_mcp_error_code(error_code: str | None) -> str:
    return {
        "-32029": "rate_limited",
        "-32030": "timeout",
        "-32031": "concurrency_limit",
    }.get(str(error_code), "mcp_error")


def _is_retryable_mcp_error(error_code: str | None) -> bool:
    return str(error_code) in {"-32029", "-32030", "-32031"}


def _text_content_message(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    first = next((item for item in content if isinstance(item, dict)), None)
    if not first:
        return None
    text = first.get("text")
    return str(text)[:500] if text else None


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

                span.set_attribute(
                    "user.id.hash", hashlib.sha256(str(user_id).encode()).hexdigest()[:16]
                )

        # Attach the span as current context so child spans nest under it
        from opentelemetry import trace

        ctx = trace.set_span_in_context(span)
        token = otel_ctx.attach(ctx)
        return span, token
    except Exception:
        return None, None
