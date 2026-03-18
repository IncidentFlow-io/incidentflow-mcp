#!/usr/bin/env python3
"""Generate a canonical OpenAPI spec for incidentflow-mcp.

This script starts from FastAPI-generated OpenAPI output (for APIRoutes), then
augments it with the custom /mcp ASGI proxy endpoint and shared components.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from incidentflow_mcp.app import create_app
from incidentflow_mcp.tools.registry import get_tool_specs
from incidentflow_mcp.tools.schemas import (
    Alert,
    AlertCluster,
    CorrelateAlertsInput,
    CorrelateAlertsOutput,
    IncidentSummaryInput,
    IncidentSummaryOutput,
    TimelineEvent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "openapi" / "openapi.yaml"


def _jsonrpc_error_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "integer"},
            "message": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
        },
    }


def _rewrite_local_defs_refs(node: Any) -> Any:
    if isinstance(node, dict):
        rewritten: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and value.startswith("#/$defs/"):
                rewritten[key] = value.replace("#/$defs/", "#/components/schemas/")
            else:
                rewritten[key] = _rewrite_local_defs_refs(value)
        return rewritten
    if isinstance(node, list):
        return [_rewrite_local_defs_refs(item) for item in node]
    return node


def _add_schema_with_defs(
    *,
    name: str,
    raw_schema: dict[str, Any],
    schemas: dict[str, Any],
) -> None:
    schema = copy.deepcopy(raw_schema)
    defs = schema.pop("$defs", {})
    for def_name, def_schema in defs.items():
        normalized_def = _rewrite_local_defs_refs(def_schema)
        if def_name not in schemas:
            schemas[def_name] = normalized_def
    schemas[name] = _rewrite_local_defs_refs(schema)


def _get_tool_specs_map() -> dict[str, Any]:
    tool_specs: dict[str, Any] = {}
    for spec in get_tool_specs():
        schema = copy.deepcopy(spec.input_schema)
        schema["title"] = f"{spec.name}Arguments"
        tool_specs[spec.name] = schema
    return tool_specs


def _build_tools_call_params_schema(tool_schemas: dict[str, Any]) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for tool_name in sorted(tool_schemas):
        variants.append(
            {
                "type": "object",
                "required": ["name", "arguments"],
                "properties": {
                    "name": {"type": "string", "enum": [tool_name]},
                    "arguments": {"$ref": f"#/components/schemas/{tool_name}Arguments"},
                },
            }
        )

    return {
        "oneOf": variants,
        "description": "Tool invocation envelope. `arguments` schema is selected by `name`.",
    }


def _build_mcp_post_request_examples() -> dict[str, Any]:
    return {
        "initialize": {
            "summary": "Initialize session",
            "value": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "local-dev", "version": "0.1.0"},
                },
            },
        },
        "toolsList": {
            "summary": "List available tools",
            "value": {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        },
        "incidentSummaryCall": {
            "summary": "Call incident_summary",
            "value": {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "incident_summary",
                    "arguments": {
                        "incident_id": "INC-001",
                        "include_timeline": True,
                        "include_affected_services": True,
                        "execution_mode": "auto",
                    },
                },
            },
        },
        "correlateAlertsCall": {
            "summary": "Call correlate_alerts",
            "value": {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "correlate_alerts",
                    "arguments": {
                        "alerts_json": '[{"alert_id":"a1","name":"HighMemoryUsage","service":"api-gateway","severity":"critical","status":"firing","fired_at":"2024-01-15T10:00:00Z","labels":{"env":"prod"}}]',
                        "window_minutes": 30,
                        "min_cluster_size": 2,
                        "execution_mode": "auto",
                    },
                },
            },
        },
        "externalStatusCall": {
            "summary": "Call external_status_check",
            "value": {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "external_status_check",
                    "arguments": {
                        "providers": ["github"],
                        "days_back": 30,
                        "wait_for_result": True,
                        "execution_mode": "async",
                        "response_mode": "compact",
                    },
                },
            },
        },
    }


def _add_components(spec: dict[str, Any]) -> None:
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    responses = components.setdefault("responses", {})

    # Existing pydantic-driven schemas from tool code.
    _add_schema_with_defs(
        name="IncidentSummaryInput",
        raw_schema=IncidentSummaryInput.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="IncidentSummaryOutput",
        raw_schema=IncidentSummaryOutput.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="CorrelateAlertsInput",
        raw_schema=CorrelateAlertsInput.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="CorrelateAlertsOutput",
        raw_schema=CorrelateAlertsOutput.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="Alert",
        raw_schema=Alert.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="AlertCluster",
        raw_schema=AlertCluster.model_json_schema(),
        schemas=schemas,
    )
    _add_schema_with_defs(
        name="TimelineEvent",
        raw_schema=TimelineEvent.model_json_schema(),
        schemas=schemas,
    )

    # Tool arguments from canonical registry.
    tool_schemas = _get_tool_specs_map()
    for tool_name, tool_schema in tool_schemas.items():
        schemas[f"{tool_name}Arguments"] = tool_schema

    schemas["InitializeParams"] = {
        "type": "object",
        "properties": {
            "protocolVersion": {"type": "string", "example": "2024-11-05"},
            "capabilities": {"type": "object", "additionalProperties": True},
            "clientInfo": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                },
                "required": ["name", "version"],
            },
        },
        "required": ["protocolVersion", "capabilities", "clientInfo"],
    }
    schemas["ToolsListParams"] = {"type": "object", "additionalProperties": False}
    schemas["ToolsCallParams"] = _build_tools_call_params_schema(tool_schemas)

    schemas["JsonRpcInitializeRequest"] = {
        "type": "object",
        "required": ["jsonrpc", "id", "method", "params"],
        "properties": {
            "jsonrpc": {"type": "string", "enum": ["2.0"]},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "method": {"type": "string", "enum": ["initialize"]},
            "params": {"$ref": "#/components/schemas/InitializeParams"},
        },
    }
    schemas["JsonRpcToolsListRequest"] = {
        "type": "object",
        "required": ["jsonrpc", "id", "method", "params"],
        "properties": {
            "jsonrpc": {"type": "string", "enum": ["2.0"]},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "method": {"type": "string", "enum": ["tools/list"]},
            "params": {"$ref": "#/components/schemas/ToolsListParams"},
        },
    }
    schemas["JsonRpcToolsCallRequest"] = {
        "type": "object",
        "required": ["jsonrpc", "id", "method", "params"],
        "properties": {
            "jsonrpc": {"type": "string", "enum": ["2.0"]},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "method": {"type": "string", "enum": ["tools/call"]},
            "params": {"$ref": "#/components/schemas/ToolsCallParams"},
        },
    }
    schemas["JsonRpcRequest"] = {
        "oneOf": [
            {"$ref": "#/components/schemas/JsonRpcInitializeRequest"},
            {"$ref": "#/components/schemas/JsonRpcToolsListRequest"},
            {"$ref": "#/components/schemas/JsonRpcToolsCallRequest"},
        ],
    }
    schemas["JsonRpcSuccessResponse"] = {
        "type": "object",
        "required": ["jsonrpc", "id", "result"],
        "properties": {
            "jsonrpc": {"type": "string", "enum": ["2.0"]},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
            "result": {"type": "object", "additionalProperties": True},
        },
    }
    schemas["JsonRpcError"] = _jsonrpc_error_schema()
    schemas["JsonRpcErrorResponse"] = {
        "type": "object",
        "required": ["jsonrpc", "id", "error"],
        "properties": {
            "jsonrpc": {"type": "string", "enum": ["2.0"]},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
            "error": {"$ref": "#/components/schemas/JsonRpcError"},
        },
    }

    schemas["UnauthorizedError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string", "example": "Missing or malformed Authorization: Bearer <token>."}},
    }
    schemas["ForbiddenScopeError"] = {
        "type": "object",
        "required": ["error", "required_scope"],
        "properties": {
            "error": {"type": "string", "enum": ["insufficient_scope"]},
            "required_scope": {"type": "string", "example": "mcp:tools:run"},
        },
    }
    schemas["ForbiddenDetailError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string", "example": "Insufficient token scope"}},
    }
    schemas["RateLimitError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string", "example": "Too Many Requests"}},
    }
    schemas["InternalServerError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string", "example": "internal server error"}},
    }

    components["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "Bearer token auth. In development with no auth provider configured, /mcp may run unprotected.",
        }
    }

    responses["UnauthorizedError"] = {
        "description": "Unauthorized",
        "headers": {"WWW-Authenticate": {"schema": {"type": "string"}, "example": "Bearer"}},
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/UnauthorizedError"},
            }
        },
    }
    responses["ForbiddenError"] = {
        "description": "Forbidden",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/ForbiddenScopeError"},
                        {"$ref": "#/components/schemas/ForbiddenDetailError"},
                    ]
                }
            }
        },
    }
    responses["RateLimitError"] = {
        "description": "Rate limited",
        "headers": {
            "Retry-After": {"schema": {"type": "integer"}},
            "X-RateLimit-Limit": {"schema": {"type": "integer"}},
            "X-RateLimit-Remaining": {"schema": {"type": "integer"}},
            "X-RateLimit-Reset": {"schema": {"type": "integer"}},
        },
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/RateLimitError"},
            }
        },
    }
    responses["InternalServerError"] = {
        "description": "Unhandled exception",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/InternalServerError"},
            }
        },
    }


def _inject_mcp_path(spec: dict[str, Any]) -> None:
    paths = spec.setdefault("paths", {})
    paths["/mcp"] = {
        "get": {
            "tags": ["mcp"],
            "operationId": "mcpGet",
            "summary": "MCP Streamable HTTP handshake",
            "description": (
                "MCP Streamable HTTP endpoint (custom ASGI proxy route). "
                "GET is supported by transport and may be used by MCP clients for handshake/session semantics."
            ),
            "security": [{"bearerAuth": []}],
            "responses": {
                "200": {
                    "description": "MCP GET response from FastMCP transport",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        },
                        "text/event-stream": {
                            "schema": {"type": "string"},
                            "example": "event: message\\ndata: {...}\\n\\n",
                        },
                    },
                },
                "401": {"$ref": "#/components/responses/UnauthorizedError"},
                "403": {"$ref": "#/components/responses/ForbiddenError"},
                "429": {"$ref": "#/components/responses/RateLimitError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
        "options": {
            "tags": ["mcp"],
            "operationId": "mcpOptions",
            "summary": "MCP CORS preflight",
            "description": "OPTIONS support for MCP endpoint (kept for CORS preflight compatibility).",
            "security": [{"bearerAuth": []}],
            "responses": {
                "200": {
                    "description": "CORS preflight response",
                },
                "401": {"$ref": "#/components/responses/UnauthorizedError"},
                "403": {"$ref": "#/components/responses/ForbiddenError"},
                "429": {"$ref": "#/components/responses/RateLimitError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
        "post": {
            "tags": ["mcp"],
            "operationId": "mcpPost",
            "summary": "MCP JSON-RPC endpoint",
            "description": (
                "Primary MCP endpoint. Accepts JSON-RPC requests such as `initialize`, `tools/list`, and `tools/call`. "
                "Some responses may stream over SSE depending on client transport/session flow."
            ),
            "security": [{"bearerAuth": []}],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/JsonRpcRequest"},
                        "examples": _build_mcp_post_request_examples(),
                    }
                },
            },
            "responses": {
                "200": {
                    "description": "JSON-RPC success or error payload",
                    "content": {
                        "application/json": {
                            "schema": {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/JsonRpcSuccessResponse"},
                                    {"$ref": "#/components/schemas/JsonRpcErrorResponse"},
                                ]
                            },
                            "examples": {
                                "success": {
                                    "summary": "Generic success response",
                                    "value": {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
                                },
                                "rateLimitToolError": {
                                    "summary": "Tool-level guard error (still HTTP 200)",
                                    "value": {
                                        "jsonrpc": "2.0",
                                        "id": 2,
                                        "error": {
                                            "code": -32029,
                                            "message": "Rate limit exceeded for tool invocation",
                                        },
                                    },
                                },
                            },
                        },
                        "text/event-stream": {
                            "schema": {"type": "string"},
                            "example": "event: message\\ndata: {...}\\n\\n",
                        },
                    },
                },
                "401": {"$ref": "#/components/responses/UnauthorizedError"},
                "403": {"$ref": "#/components/responses/ForbiddenError"},
                "429": {"$ref": "#/components/responses/RateLimitError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
    }


def _annotate_existing_paths(spec: dict[str, Any]) -> None:
    paths = spec.get("paths", {})

    public_paths = {"/install.sh", "/healthz", "/readyz", "/metrics"}
    ops_tags = {
        "/install.sh": "ops",
        "/healthz": "ops",
        "/readyz": "ops",
        "/metrics": "ops",
    }

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue

            if "operationId" not in operation or not operation["operationId"]:
                operation["operationId"] = f"{method}_{path.strip('/').replace('/', '_').replace('.', '_')}"

            if path in public_paths:
                operation["security"] = []
                operation["tags"] = [ops_tags[path]]

            responses = operation.setdefault("responses", {})
            if "500" not in responses:
                responses["500"] = {"$ref": "#/components/responses/InternalServerError"}

            if path == "/metrics":
                resp_200 = responses.get("200")
                if isinstance(resp_200, dict):
                    content = resp_200.setdefault("content", {})
                    content.setdefault("text/plain", {"schema": {"type": "string"}})


def generate_openapi() -> dict[str, Any]:
    app = create_app()
    spec = app.openapi()

    spec["info"]["title"] = "IncidentFlow MCP API"
    spec["info"]["description"] = (
        "Canonical API specification for IncidentFlow MCP. "
        "Generated from FastAPI routes and MCP tool metadata."
    )

    spec["tags"] = [
        {"name": "ops", "description": "Operational/public endpoints"},
        {"name": "mcp", "description": "MCP Streamable HTTP transport endpoint"},
    ]

    _add_components(spec)
    _inject_mcp_path(spec)
    _annotate_existing_paths(spec)

    return spec


def main() -> None:
    spec = generate_openapi()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        yaml.safe_dump(spec, sort_keys=False, allow_unicode=False, width=100),
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
