"""Versioned response contract for IncidentFlow MCP tools.

Every MCP tool response is a single canonical envelope:

    {
      "api_version": "v1",
      "schema_version": "1.0",
      "schema_id": "incidentflow.<tool>.response",
      "status": "success" | "error",
      "request_id": "req_...",
      "data": { ... } | null,
      "error": { "code", "message", "retryable", "details" } | null,
      "meta": { "generated_at", "truncated", "warnings": [] }
    }

The envelope shape, the common error schema, and per-tool ``data`` schemas are
JSON Schema Draft 2020-12. Output schemas published to MCP clients are generated
*inline* (envelope + tool-specific data merged, no external ``$ref``) so a client
that cannot resolve remote references still receives a complete, self-contained
schema.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from incidentflow_mcp.tools.output_models import TOOL_OUTPUT_MODELS, schema_mode_for
from incidentflow_mcp.tools.registry import ToolSpec

# --- contract-level constants ----------------------------------------------
API_VERSION = "v1"
SCHEMA_VERSION = "1.0"
CONTRACT_VERSION = "1.0"
SUPPORTED_API_VERSIONS: tuple[str, ...] = ("v1",)
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("1.0",)
DEPRECATED_API_VERSIONS: tuple[str, ...] = ()

SCHEMA_ID_NAMESPACE = "incidentflow"
ERROR_SCHEMA_ID = f"{SCHEMA_ID_NAMESPACE}.common.error"
ENVELOPE_SCHEMA_ID = f"{SCHEMA_ID_NAMESPACE}.common.envelope"

TOOL_SCHEMA_BASE_URL = "https://incidentflow.io/schemas/tools"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# Keys the tool handler must never place at the top level of its raw payload;
# they belong to the envelope and would be shadowed.
RESERVED_ENVELOPE_KEYS = frozenset(
    {
        "api_version",
        "schema_version",
        "schema_id",
        "status",
        "request_id",
        "data",
        "error",
        "meta",
    }
)


class ErrorCode(StrEnum):
    """The single canonical set of tool error codes (req #10).

    The same value appears in ``structuredContent.error.code``, in the MCP
    ``isError`` outcome, in application logs, and in metrics/traces.
    """

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    INTEGRATION_UNAVAILABLE = "INTEGRATION_UNAVAILABLE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.TIMEOUT,
        ErrorCode.UPSTREAM_ERROR,
        ErrorCode.INTEGRATION_UNAVAILABLE,
    }
)


def default_retryable(code: ErrorCode) -> bool:
    """Return the default retryability for one canonical error code."""

    return code in _RETRYABLE_CODES


# --- schema-id helpers ------------------------------------------------------
def _slug(tool_name: str) -> str:
    return tool_name.replace("_", "-")


def response_schema_id(tool_name: str) -> str:
    """Stable response schema id for one tool, e.g. ``incidentflow.mcp-version.response``."""

    return f"{SCHEMA_ID_NAMESPACE}.{_slug(tool_name)}.response"


def request_schema_id(tool_name: str) -> str:
    """Stable request schema id for one tool, e.g. ``incidentflow.mcp-version.request``."""

    return f"{SCHEMA_ID_NAMESPACE}.{_slug(tool_name)}.request"


# Backwards-compatible alias used by older callers / tests.
def schema_id_for_tool(tool_name: str) -> str:
    return response_schema_id(tool_name)


# --- runtime envelope builders ---------------------------------------------
def generate_request_id() -> str:
    """Generate a request id with the ``req_`` prefix."""

    return f"req_{uuid.uuid4().hex[:24]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_meta(*, truncated: bool = False, warnings: list[str] | None = None) -> dict[str, Any]:
    """Build the envelope ``meta`` block."""

    return {
        "generated_at": _now_iso(),
        "truncated": bool(truncated),
        "warnings": list(warnings or []),
    }


def success_envelope(
    data: Any,
    *,
    tool_name: str,
    request_id: str | None = None,
    truncated: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a success envelope wrapping ``data`` for one tool."""

    return {
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "schema_id": response_schema_id(tool_name),
        "status": "success",
        "request_id": request_id or generate_request_id(),
        "data": data,
        "error": None,
        "meta": build_meta(truncated=truncated, warnings=warnings),
    }


def error_envelope(
    *,
    tool_name: str,
    code: ErrorCode | str,
    message: str,
    retryable: bool | None = None,
    details: Any | None = None,
    request_id: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build an error envelope for one tool."""

    resolved_code = code if isinstance(code, ErrorCode) else ErrorCode(str(code))
    resolved_retryable = default_retryable(resolved_code) if retryable is None else bool(retryable)
    error: dict[str, Any] = {
        "code": resolved_code.value,
        "message": message,
        "retryable": resolved_retryable,
        "details": details,
    }
    return {
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "schema_id": response_schema_id(tool_name),
        "status": "error",
        "request_id": request_id or generate_request_id(),
        "data": None,
        "error": error,
        "meta": build_meta(warnings=warnings),
    }


# --- JSON Schema generation -------------------------------------------------
def error_schema() -> dict[str, Any]:
    """Draft 2020-12 schema for the common error object."""

    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": schema_url(f"{ERROR_SCHEMA_ID}.schema.json"),
        "title": "IncidentFlow tool error",
        "type": "object",
        "additionalProperties": False,
        "required": ["code", "message", "retryable"],
        "properties": {
            "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
            "message": {"type": "string"},
            "retryable": {"type": "boolean"},
            "details": {"type": ["object", "array", "string", "number", "boolean", "null"]},
        },
    }


def _meta_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["generated_at", "truncated", "warnings"],
        "properties": {
            "generated_at": {"type": "string"},
            "truncated": {"type": "boolean"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _generic_data_schema() -> dict[str, Any]:
    # Last-resort placeholder for a tool with no registered model. A coverage
    # test (tests/test_schema_coverage.py) fails if any operational tool relies
    # on this, so it never silently masks a missing schema.
    return {"type": ["object", "array", "string", "number", "boolean", "null"]}


def _strip_titles(node: Any) -> Any:
    """Recursively drop Pydantic-generated ``title`` *annotations* (schema noise).

    Only removes ``title`` when its value is a string (a JSON Schema annotation).
    A property literally named ``title`` (whose value is a subschema dict) is left
    intact — otherwise fields like ``knowledge_upsert.title`` would vanish.
    """

    if isinstance(node, dict):
        if isinstance(node.get("title"), str):
            node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)
    return node


def data_schema_for(tool_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(data_schema, defs)`` for one tool from its registered model.

    ``defs`` are the nested-model definitions to be lifted onto the envelope's
    top-level ``$defs`` so local ``$ref`` targets resolve (self-contained; no
    external references — review #6).
    """

    entry = TOOL_OUTPUT_MODELS.get(tool_name)
    if entry is None:
        return _generic_data_schema(), {}
    if isinstance(entry, dict):
        raw = copy.deepcopy(entry)
    elif isinstance(entry, type) and issubclass(entry, BaseModel):
        raw = entry.model_json_schema(mode="serialization")
    else:  # pragma: no cover - registry misuse
        raise TypeError(f"Unsupported output model entry for {tool_name}: {type(entry)!r}")
    defs = raw.pop("$defs", {})
    return _strip_titles(raw), _strip_titles(defs)


def envelope_schema(
    *,
    tool_name: str | None = None,
    data_schema: dict[str, Any] | None = None,
    defs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an inline Draft 2020-12 envelope schema.

    When ``tool_name`` is given the tool-specific ``data`` schema is inlined and
    the ``schema_id`` is pinned; otherwise a generic envelope schema is returned.
    Nested-model ``defs`` are lifted onto the envelope's top-level ``$defs``.
    """

    resolved_data = data_schema if data_schema is not None else _generic_data_schema()
    schema_id_const: dict[str, Any] = {}
    schema_ref = ENVELOPE_SCHEMA_ID
    if tool_name is not None:
        schema_ref = response_schema_id(tool_name)
        schema_id_const = {"const": schema_ref}

    schema: dict[str, Any] = {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": schema_url(f"{schema_ref}.schema.json"),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "api_version",
            "schema_version",
            "schema_id",
            "status",
            "request_id",
            "data",
            "error",
            "meta",
        ],
        "properties": {
            "api_version": {"type": "string", "enum": list(SUPPORTED_API_VERSIONS)},
            "schema_version": {"type": "string", "enum": list(SUPPORTED_SCHEMA_VERSIONS)},
            "schema_id": {"type": "string", **schema_id_const},
            "status": {"type": "string", "enum": ["success", "error"]},
            "request_id": {"type": "string"},
            # data is present on success and null on error
            "data": {"anyOf": [resolved_data, {"type": "null"}]},
            "error": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["code", "message", "retryable"],
                        "properties": {
                            "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
                            "message": {"type": "string"},
                            "retryable": {"type": "boolean"},
                            "details": {
                                "type": [
                                    "object",
                                    "array",
                                    "string",
                                    "number",
                                    "boolean",
                                    "null",
                                ]
                            },
                        },
                    },
                    {"type": "null"},
                ]
            },
            "meta": _meta_schema(),
        },
    }
    if defs:
        schema["$defs"] = defs
    return schema


def build_output_schema(tool_name: str) -> dict[str, Any]:
    """Return the inline output schema registered with the MCP framework for one tool."""

    data_schema, defs = data_schema_for(tool_name)
    return envelope_schema(tool_name=tool_name, data_schema=data_schema, defs=defs)


def schema_mode(tool_name: str) -> str:
    """Return ``"strict"`` or ``"permissive"`` for one tool's data schema."""

    return schema_mode_for(tool_name)


def schema_url(schema_name: str) -> str:
    """Return the canonical URL for one generated schema file."""

    return f"{TOOL_SCHEMA_BASE_URL}/{schema_name}"


def capability_schema_metadata(tool_name: str) -> dict[str, str]:
    """Per-tool schema metadata block for ``incidentflow_capabilities`` (req #8)."""

    return {
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input_schema_id": request_schema_id(tool_name),
        "output_schema_id": response_schema_id(tool_name),
        "error_schema_id": ERROR_SCHEMA_ID,
    }


# --- schema catalog (served over HTTP) -------------------------------------
def all_schema_ids() -> list[str]:
    """Every published schema id: common envelope + error + one per tool."""

    ids = [ENVELOPE_SCHEMA_ID, ERROR_SCHEMA_ID]
    ids.extend(sorted(response_schema_id(name) for name in TOOL_OUTPUT_MODELS))
    return ids


def get_schema(schema_id: str) -> dict[str, Any] | None:
    """Return one generated JSON Schema by id, or ``None`` if unknown."""

    if schema_id == ENVELOPE_SCHEMA_ID:
        return envelope_schema()
    if schema_id == ERROR_SCHEMA_ID:
        return error_schema()
    for name in TOOL_OUTPUT_MODELS:
        if response_schema_id(name) == schema_id:
            return build_output_schema(name)
    return None


def schema_catalog(base_url: str = "") -> dict[str, Any]:
    """Catalog document for ``GET /schemas``."""

    base = base_url.rstrip("/")
    return {
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "schemas": [
            {"schema_id": sid, "url": f"{base}/schemas/{sid}" if base else f"/schemas/{sid}"}
            for sid in all_schema_ids()
        ],
    }


# --- offline export ---------------------------------------------------------
def export_tool_schemas(specs: list[ToolSpec], output_dir: Path) -> list[Path]:
    """Generate JSON Schema files: common error, generic envelope, per-tool envelopes."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _write(name: str, schema: dict[str, Any]) -> None:
        path = output_dir / name
        path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    _write("incidentflow.common.error.schema.json", error_schema())
    _write("incidentflow.common.envelope.schema.json", envelope_schema())

    for spec in sorted(specs, key=lambda item: item.name):
        _write(
            f"{response_schema_id(spec.name)}.schema.json",
            build_output_schema(spec.name),
        )

    return written
