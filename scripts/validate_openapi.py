#!/usr/bin/env python3
"""Validate the generated OpenAPI spec.

Uses `openapi-spec-validator` when available; otherwise performs strict
structural validation checks that run fully offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

try:
    from openapi_spec_validator import validate_spec as _validate_spec  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency path
    _validate_spec = None

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "openapi" / "openapi.yaml"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _offline_validate(spec: dict[str, Any]) -> None:
    _assert(isinstance(spec, dict), "Spec must be a mapping")
    _assert(isinstance(spec.get("openapi"), str), "Missing openapi version")
    _assert(isinstance(spec.get("info"), dict), "Missing info section")
    _assert(isinstance(spec.get("paths"), dict), "Missing paths section")

    required_paths = {"/install.sh", "/healthz", "/readyz", "/metrics", "/mcp"}
    actual_paths = set(spec["paths"].keys())
    missing = required_paths - actual_paths
    _assert(not missing, f"Missing required paths: {sorted(missing)}")

    mcp_ops = spec["paths"]["/mcp"]
    _assert(all(method in mcp_ops for method in ("get", "post", "options")), "/mcp must define GET/POST/OPTIONS")

    components = spec.get("components", {})
    _assert(isinstance(components, dict), "components must be an object")
    sec = components.get("securitySchemes", {})
    _assert("bearerAuth" in sec, "Missing components.securitySchemes.bearerAuth")


def main() -> None:
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    _offline_validate(spec)
    if _validate_spec is not None:
        _validate_spec(spec)
        print(f"OpenAPI spec is valid (structural + openapi-spec-validator): {SPEC_PATH}")
        return
    print(f"OpenAPI spec is valid (structural offline checks): {SPEC_PATH}")


if __name__ == "__main__":
    main()
