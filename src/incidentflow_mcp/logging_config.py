"""
Structured logging setup.

Call `configure_logging()` once at startup. All other modules should use:

    import logging
    logger = logging.getLogger(__name__)
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "slack_sdk",
    "sse_starlette",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "watchfiles",
    "watchfiles.main",
)

_STANDARD_LOG_RECORD_KEYS = set(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"asctime", "message"}

_IGNORED_EXTRA_KEYS = {"color_message"}


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(r"(redis://)([^:@\s]+:)?([^@\s]+)@", r"\1***@", value)
    return re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)=([^\s,;]+)",
        r"\1=***",
        redacted,
    )


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_sensitive_text(str(record.msg))
        if record.args:
            record.args = tuple(
                _redact_sensitive_text(item) if isinstance(item, str) else item
                for item in record.args
            )
        return True


class _TraceContextFilter(logging.Filter):
    """Inject trace_id and span_id from the active OTEL span into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id = ""
        span_id = ""
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                trace_id = format(ctx.trace_id, "032x")
                span_id = format(ctx.span_id, "016x")
        except Exception:
            pass
        record.trace_id = trace_id
        record.span_id = span_id
        return True


def _json_safe(value: object) -> object:
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return _redact_sensitive_text(str(value))


class _JsonFormatter(logging.Formatter):
    def __init__(
        self,
        *,
        service: str,
        service_version: str,
        environment: str,
    ) -> None:
        super().__init__()
        self._service = service
        self._service_version = service_version
        self._environment = environment

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        _ = datefmt
        return (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": self._service,
            "service_version": self._service_version,
            "environment": self._environment,
            "logger": record.name,
            "event": _redact_sensitive_text(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_KEYS or key in payload or key in _IGNORED_EXTRA_KEYS:
                continue
            if value is None or value == "":
                continue
            payload[key] = _json_safe(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def __init__(
        self,
        *,
        service: str,
        service_version: str,
        environment: str,
    ) -> None:
        super().__init__()
        self._service = service
        self._service_version = service_version
        self._environment = environment

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        _ = datefmt
        return (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            self.formatTime(record),
            f"{record.levelname:<8}",
            self._service,
            f"service_version={self._service_version}",
            f"environment={self._environment}",
            record.name,
        ]

        trace_id = getattr(record, "trace_id", "")
        span_id = getattr(record, "span_id", "")
        if trace_id:
            parts.append(f"trace_id={trace_id}")
        if span_id:
            parts.append(f"span_id={span_id}")

        parts.append(_redact_sensitive_text(record.getMessage()))

        ignored = _STANDARD_LOG_RECORD_KEYS | _IGNORED_EXTRA_KEYS | {"trace_id", "span_id"}
        for key, value in record.__dict__.items():
            if key in ignored:
                continue
            if value is None or value == "":
                continue
            parts.append(f"{key}={_redact_sensitive_text(str(value))}")

        formatted = "  ".join(parts)
        if record.exc_info:
            formatted += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            formatted += "\n" + self.formatStack(record.stack_info)
        return formatted


def configure_logging(
    level: str = "info",
    library_level: str = "warning",
    log_format: str = "text",
    service: str = "incidentflow-mcp",
    service_version: str = "0.1.0",
    environment: str = "dev",
) -> None:
    """
    Configure root logger with a structured format suitable for both
    local development (human-readable) and container environments.

    Use log_format="json" for one JSON object per line in containers and CI.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    handler.addFilter(_RedactionFilter())
    handler.addFilter(_TraceContextFilter())

    if log_format.lower() == "json":
        fmt: logging.Formatter = _JsonFormatter(
            service=service,
            service_version=service_version,
            environment=environment,
        )
    else:
        fmt = _TextFormatter(
            service=service,
            service_version=service_version,
            environment=environment,
        )
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()
    root.addHandler(handler)

    library_numeric_level = getattr(logging, library_level.upper(), logging.WARNING)
    for logger_name in _NOISY_LOGGERS:
        library_logger = logging.getLogger(logger_name)
        library_logger.setLevel(library_numeric_level)
        library_logger.handlers.clear()
        library_logger.propagate = True

    logging.getLogger("uvicorn.access").disabled = True
