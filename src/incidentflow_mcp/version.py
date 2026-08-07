"""Single source of truth for the incidentflow-mcp service version.

Runtime version, logs, OTEL resource, ops-health and the ``mcp_version`` tool all
resolve their version through :func:`resolve_service_version` so telemetry can
never drift from the value reported to clients.

Precedence:
1. build-time metadata injected into the image (``MCP_BUILD_VERSION`` / ``MCP_BUILD_TAG``);
2. the installed package version (``importlib.metadata`` reading ``pyproject``);
3. the configured ``mcp_server_version`` fallback.
"""

from __future__ import annotations

from importlib import metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from incidentflow_mcp.config import Settings

_DISTRIBUTION_NAME = "incidentflow-mcp"


def normalize_version(raw: str | None, fallback: str = "") -> str:
    """Strip release-lane prefixes (``dev-v`` / ``v``) from a raw version string."""

    version = (raw or "").strip() or fallback
    if version.startswith("dev-v"):
        return version.removeprefix("dev-v")
    if version.startswith("v"):
        return version.removeprefix("v")
    return version


def package_version() -> str | None:
    """Return the installed package version from build metadata, if available."""

    try:
        return metadata.version(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
        return None


def resolve_service_version(settings: Settings) -> str:
    """Resolve the canonical service version from a single precedence chain."""

    build_version = (settings.mcp_build_version or settings.mcp_build_tag or "").strip()
    if build_version:
        return normalize_version(build_version)

    installed = package_version()
    if installed:
        return normalize_version(installed)

    return normalize_version(settings.mcp_server_version, "0.0.0")
