"""
Structured logging setup.

Call `configure_logging()` once at startup. All other modules should use:

    import logging
    logger = logging.getLogger(__name__)
"""

import logging
import sys

_NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "slack_sdk",
    "sse_starlette",
)


def configure_logging(level: str = "info", library_level: str = "warning") -> None:
    """
    Configure root logger with a structured format suitable for both
    local development (human-readable) and container environments.

    Future: swap the Formatter for a JSON formatter (e.g. python-json-logger)
    when aggregating logs in a centralised system.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()
    root.addHandler(handler)

    library_numeric_level = getattr(logging, library_level.upper(), logging.WARNING)
    for logger_name in _NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(library_numeric_level)
