"""OpenTelemetry tracing setup for incidentflow-mcp.

Intentionally no-op safe: if opentelemetry packages are absent the module
loads and every helper becomes a noop so local tests and development work
without the full OTEL SDK installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

_TRACER_NAME = "incidentflow.mcp"
_initialized = False
_provider = None


def configure_tracing(
    *,
    service_name: str = "incidentflow-mcp",
    service_version: str = "0.0.0",
    environment: str = "development",
    otlp_endpoint: str = "otel-collector.observability.svc.cluster.local:4317",
    k8s_namespace: str | None = None,
    enabled: bool = True,
) -> None:
    """Initialize the OTEL SDK and register a global TracerProvider.

    Safe to call multiple times; subsequent calls after the first are no-ops.
    """
    global _initialized
    if _initialized or not enabled:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.info("opentelemetry packages not installed; tracing disabled")
        return

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": service_version,
        "deployment.environment": _normalize_env(environment),
    }
    if k8s_namespace:
        resource_attrs["k8s.namespace.name"] = k8s_namespace

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        logger.debug("HTTPX auto-instrumentation unavailable")

    _initialized = True
    _provider = provider
    logger.info(
        "otel tracing enabled service=%s endpoint=%s env=%s",
        service_name,
        otlp_endpoint,
        _normalize_env(environment),
    )


def get_tracer() -> Tracer:
    """Return the module-scoped tracer; noop tracer when OTEL is unavailable."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(_TRACER_NAME)
    except ImportError:
        return _NoopTracer()  # type: ignore[return-value]


def inject_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject W3C traceparent/tracestate into an outbound HTTP headers dict."""
    try:
        from opentelemetry.propagate import inject

        inject(headers)
    except Exception:
        pass
    return headers


def current_trace_context() -> tuple[str, str]:
    """Return (trace_id_hex, span_id_hex) of the active span, or ('', '')."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return "", ""


def _normalize_env(value: str) -> str:
    v = value.strip().lower()
    if v in {"production", "prod"}:
        return "prod"
    if v in {"development", "dev"}:
        return "dev"
    return v or "unknown"


class _NoopSpan:
    """No-op span returned by _NoopTracer so callers never get None."""

    def set_attribute(self, *a, **kw): ...
    def set_status(self, *a, **kw): ...
    def record_exception(self, *a, **kw): ...
    def add_event(self, *a, **kw): ...
    def end(self): ...
    def __enter__(self):
        return self

    def __exit__(self, *a): ...


class _NoopTracer:
    """Minimal stand-in so callers don't need to guard against None."""

    def start_as_current_span(self, name: str, **kwargs):
        from contextlib import nullcontext

        return nullcontext(_NoopSpan())

    def start_span(self, name: str, **kwargs):
        return _NoopSpan()


def instrument_fastapi_app(app: object) -> None:
    """Instrument a FastAPI app with the global TracerProvider.

    Must be called after FastAPI() is constructed but before the first request.
    Safe to call when tracing is disabled — becomes a no-op.
    """
    if _provider is None:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)  # type: ignore[arg-type]
    except Exception:
        logger.debug("FastAPI auto-instrumentation unavailable")
