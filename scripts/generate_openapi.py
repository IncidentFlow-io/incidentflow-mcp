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
DOCS_DOWNLOAD_DIR = REPO_ROOT / "fern" / "assets" / "downloads"

_RELEASE_BLOCKED_TOOLS = {
    "memory_search_similar_incidents",
    "memory_get_service_context",
    "memory_upsert_incident_summary",
    "memory_find_runbook",
}
_CONDITIONAL_TOOLS = {
    "external_status_check",
    "slack_alerts_list",
    "slack_alert_thread_get",
    "incident_thread_summary",
    "k8s_connection_health",
    "k8s_cluster_overview",
    "k8s_namespace_overview",
    "k8s_rbac_check",
    "k8s_agent_status",
    "k8s_list_namespaces",
    "k8s_list_pods",
    "k8s_get_pod",
    "k8s_get_pod_logs",
    "k8s_list_events",
    "k8s_list_deployments",
    "k8s_list_services",
    "k8s_get_rollout_status",
    "k8s_show_unhealthy_pods",
    "k8s_analyze_workload",
    "k8s_describe_pod",
    "k8s_debug_pod",
}


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
                        "execution_mode": "sync",
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
                        "alerts": [
                            {
                                "alert_id": "juniper-qa-001",
                                "name": "CheckoutLatencyHigh",
                                "service": "checkout-api",
                                "severity": "high",
                                "status": "firing",
                                "fired_at": "2026-08-02T09:00:00Z",
                                "labels": {"environment": "qa", "tenant": "junipercart"},
                            }
                        ],
                        "window_minutes": 30,
                        "min_cluster_size": 1,
                        "execution_mode": "sync",
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
    specs_by_name = {spec.name: spec for spec in get_tool_specs()}
    for tool_name, tool_schema in tool_schemas.items():
        spec = specs_by_name[tool_name]
        if not spec.submission_ready:
            availability = "not-public"
        elif tool_name in _RELEASE_BLOCKED_TOOLS:
            availability = "release-blocked"
        elif tool_name in _CONDITIONAL_TOOLS:
            availability = "conditional"
        else:
            availability = "available"
        tool_schema["x-incidentflow-title"] = spec.title
        tool_schema["x-incidentflow-availability"] = availability
        tool_schema["x-mcp-behavior"] = copy.deepcopy(spec.annotations)
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
        "properties": {
            "detail": {
                "type": "string",
                "example": "Missing or malformed Authorization: Bearer <token>.",
            }
        },
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
    schemas["ServiceUnavailableError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {
            "detail": {
                "type": "string",
                "example": "Token verification service unavailable",
            }
        },
    }
    schemas["HealthResponse"] = {
        "type": "object",
        "required": ["status", "service", "version", "environment"],
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "service": {"type": "string", "example": "incidentflow-mcp"},
            "version": {"type": "string", "example": "0.1.0"},
            "environment": {"type": "string", "example": "production"},
        },
    }
    schemas["ReadinessResponse"] = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string", "enum": ["ready"]}},
    }
    schemas["OAuthProtectedResourceMetadata"] = {
        "type": "object",
        "required": ["resource", "authorization_servers", "scopes_supported"],
        "properties": {
            "resource": {
                "type": "string",
                "format": "uri",
                "example": "https://mcp.incidentflow.io/mcp",
            },
            "authorization_servers": {
                "type": "array",
                "items": {"type": "string", "format": "uri"},
                "minItems": 1,
            },
            "scopes_supported": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["mcp:read", "mcp:tools:run", "admin"],
                },
            },
        },
    }
    schemas["NotFoundError"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string", "example": "Not found"}},
    }

    components["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "Bearer token auth. Production clients should use IncidentFlow OAuth or a "
                "workspace-scoped managed token and request mcp:read plus mcp:tools:run. "
                "Send credentials only in the Authorization header."
            ),
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
    responses["ServiceUnavailableError"] = {
        "description": "Required token-verification service unavailable; verification fails closed",
        "headers": {"Retry-After": {"schema": {"type": "integer"}}},
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ServiceUnavailableError"},
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
                "GET is supported by transport and may be used by MCP clients for "
                "handshake/session semantics."
            ),
            "x-incidentflow-availability": "candidate",
            "x-incidentflow-human-review": "pending",
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
                "503": {"$ref": "#/components/responses/ServiceUnavailableError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
        "options": {
            "tags": ["mcp"],
            "operationId": "mcpOptions",
            "summary": "MCP CORS preflight",
            "description": (
                "OPTIONS support for MCP endpoint (kept for CORS preflight compatibility)."
            ),
            "x-incidentflow-availability": "candidate",
            "x-incidentflow-human-review": "pending",
            "security": [{"bearerAuth": []}],
            "responses": {
                "200": {
                    "description": "CORS preflight response",
                },
                "401": {"$ref": "#/components/responses/UnauthorizedError"},
                "403": {"$ref": "#/components/responses/ForbiddenError"},
                "429": {"$ref": "#/components/responses/RateLimitError"},
                "503": {"$ref": "#/components/responses/ServiceUnavailableError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
        "post": {
            "tags": ["mcp"],
            "operationId": "mcpPost",
            "summary": "MCP JSON-RPC endpoint",
            "description": (
                "Primary MCP endpoint. Accepts JSON-RPC requests such as `initialize`, "
                "`tools/list`, and `tools/call`. "
                "Use a bearer credential with mcp:read and mcp:tools:run for a tool-using client. "
                "Some responses may stream over SSE depending on client transport/session flow. "
                "Default transport and tool limits are deployment policy; honor 429 response "
                "headers."
            ),
            "x-incidentflow-availability": "candidate",
            "x-incidentflow-human-review": "pending",
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
                "503": {"$ref": "#/components/responses/ServiceUnavailableError"},
                "500": {"$ref": "#/components/responses/InternalServerError"},
            },
        },
    }


def _annotate_existing_paths(spec: dict[str, Any]) -> None:
    paths = spec.get("paths", {})

    public_paths = {
        "/install.sh",
        "/healthz",
        "/readyz",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/{challenge_path}",
    }
    ops_tags = {
        "/install.sh": "ops",
        "/healthz": "ops",
        "/readyz": "ops",
        "/metrics": "ops",
        "/.well-known/oauth-protected-resource": "ops",
        "/.well-known/oauth-protected-resource/mcp": "ops",
        "/.well-known/{challenge_path}": "ops",
    }

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue

            if "operationId" not in operation or not operation["operationId"]:
                normalized_path = path.strip("/").replace("/", "_").replace(".", "_")
                operation["operationId"] = f"{method}_{normalized_path}"

            if path in ops_tags:
                operation["tags"] = [ops_tags[path]]

            if path in public_paths:
                operation["security"] = []

            if path == "/metrics":
                operation["security"] = [{"bearerAuth": []}]
                operation["description"] = (
                    "Prometheus text exposition. A production deployment requires bearer "
                    "authentication unless the caller is in a configured trusted monitoring CIDR."
                )

            operation["x-incidentflow-availability"] = "candidate"
            operation["x-incidentflow-human-review"] = "pending"

            responses = operation.setdefault("responses", {})
            if "500" not in responses:
                responses["500"] = {"$ref": "#/components/responses/InternalServerError"}

    paths["/install.sh"]["get"]["responses"]["200"] = {
        "description": "Installer shell script generated for the current MCP origin",
        "headers": {
            "Content-Disposition": {
                "schema": {"type": "string"},
                "example": 'inline; filename="install.sh"',
            },
            "Cache-Control": {"schema": {"type": "string"}, "example": "no-store"},
        },
        "content": {
            "text/x-shellscript": {
                "schema": {"type": "string"},
                "example": "#!/usr/bin/env bash\n# Inspect before execution.\n",
            }
        },
    }
    paths["/healthz"]["get"]["responses"]["200"] = {
        "description": "Service process is alive",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/HealthResponse"}}
        },
    }
    paths["/readyz"]["get"]["responses"]["200"] = {
        "description": "Service is ready to receive requests",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ReadinessResponse"}}
        },
    }
    paths["/metrics"]["get"]["responses"]["200"] = {
        "description": "Prometheus text exposition",
        "content": {
            "text/plain": {
                "schema": {"type": "string"},
                "example": "# HELP mcp_tool_requests_total MCP tool requests\n",
            }
        },
    }

    metadata_paths = (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    )
    for path in metadata_paths:
        paths[path]["get"]["description"] = (
            "Discover the authorization server, canonical MCP resource, and supported scopes."
        )
        paths[path]["get"]["responses"]["200"] = {
            "description": "OAuth protected-resource metadata",
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/OAuthProtectedResourceMetadata"
                    },
                    "example": {
                        "resource": "https://mcp.incidentflow.io/mcp",
                        "authorization_servers": ["https://api.incidentflow.io"],
                        "scopes_supported": ["mcp:read", "mcp:tools:run", "admin"],
                    },
                }
            },
        }

    verification = paths["/.well-known/{challenge_path}"]["get"]
    verification["description"] = (
        "Return a configured OpenAI Apps domain-verification token only for the exact configured "
        "challenge path. This is service verification, not a customer API operation."
    )
    verification["responses"]["200"] = {
        "description": "Configured domain-verification token",
        "content": {"text/plain": {"schema": {"type": "string"}}},
    }
    verification["responses"]["404"] = {
        "description": "Challenge path is not configured",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/NotFoundError"}
            }
        },
    }


def generate_openapi() -> dict[str, Any]:
    app = create_app()
    spec = app.openapi()

    spec["info"]["title"] = "IncidentFlow MCP API"
    spec["info"]["description"] = (
        "Canonical customer-facing contract for the IncidentFlow MCP Streamable HTTP service. "
        "Generated from FastAPI routes and canonical MCP tool metadata. Tool schemas carry "
        "availability and behavior extensions; source presence does not imply public availability."
    )
    spec["info"]["x-incidentflow-release-status"] = "candidate"
    spec["info"]["x-incidentflow-human-review"] = "pending"
    spec["servers"] = [
        {
            "url": "https://mcp.incidentflow.io",
            "description": "IncidentFlow MCP production origin",
        }
    ]
    spec["externalDocs"] = {
        "description": "IncidentFlow MCP documentation",
        "url": "https://docs.incidentflow.io",
    }

    spec["tags"] = [
        {
            "name": "mcp",
            "description": "Authenticated MCP Streamable HTTP transport and JSON-RPC operations",
        },
        {
            "name": "ops",
            "description": "Public service discovery, health, installer, and metrics endpoints",
        },
    ]

    _add_components(spec)
    _inject_mcp_path(spec)
    _annotate_existing_paths(spec)

    return spec


def main() -> None:
    spec = generate_openapi()
    rendered = yaml.safe_dump(spec, sort_keys=False, allow_unicode=False, width=100)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")

    version = str(spec["info"]["version"])
    if not version or any(character in version for character in "/\\"):
        raise ValueError(f"Unsafe OpenAPI version for download filename: {version!r}")
    download_path = DOCS_DOWNLOAD_DIR / f"incidentflow-mcp-openapi-{version}.yaml"
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Wrote {download_path}")


if __name__ == "__main__":
    main()
