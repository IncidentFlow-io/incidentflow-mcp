"""Strict per-tool ``data`` JSON Schemas (Draft 2020-12).

Each schema describes the ``data`` payload for one MCP tool response. They are
composed into the shared response envelope by :mod:`incidentflow_mcp.tools.contracts`.

Design rules (see docs/api-versioning.md):
- one schema per tool — never a single blanket ``additionalProperties: true`` for all;
- tools whose payload we fully own are strict (``additionalProperties: false``);
- tools that pass an upstream payload through (Argo CD health, knowledge search,
  polymorphic async jobs) declare their known fields but tolerate upstream additions
  so a live response never fails output validation.
"""

from __future__ import annotations

from typing import Any

_ISO_DATETIME: dict[str, Any] = {"type": ["string", "null"]}

# --- mcp_version -----------------------------------------------------------
# Fully owned, strict.
MCP_VERSION_DATA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "service",
        "service_version",
        "api_version",
        "contract_version",
        "supported_api_versions",
        "supported_schema_versions",
        "deprecated_api_versions",
        "environment",
    ],
    "properties": {
        "service": {"type": "string"},
        "service_version": {"type": "string"},
        "api_version": {"type": "string"},
        "contract_version": {"type": "string"},
        "schema_version": {"type": "string"},
        "supported_api_versions": {"type": "array", "items": {"type": "string"}},
        "supported_schema_versions": {"type": "array", "items": {"type": "string"}},
        "deprecated_api_versions": {"type": "array", "items": {"type": "string"}},
        "environment": {"type": "string"},
        "tag": {"type": ["string", "null"]},
        "commit": {"type": ["string", "null"]},
        "built_at": {"type": ["string", "null"]},
        "tools": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "registered": {"type": "integer"},
                "operational": {"type": "integer"},
                "meta": {"type": "integer"},
            },
        },
        "image": {"type": "object"},
    },
}

# --- k8s_agent_status ------------------------------------------------------
# Fully owned, strict. `status` is the integration lifecycle; `healthy` is the
# normalized boolean integration-state (no `ok` / `agent_online` duplication).
K8S_AGENT_STATUS_DATA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "healthy", "checked_at"],
    "properties": {
        "status": {"type": "string", "enum": ["connected", "offline"]},
        "healthy": {"type": "boolean"},
        "cluster_id": {"type": ["string", "null"]},
        "cluster_name": {"type": ["string", "null"]},
        "environment": {"type": ["string", "null"]},
        "agent_id": {"type": ["string", "null"]},
        "agent_version": {"type": ["string", "null"]},
        "agent_status": {"type": ["string", "null"]},
        "last_seen_at": _ISO_DATETIME,
        "last_heartbeat_at": _ISO_DATETIME,
        "checked_at": {"type": "string"},
        "error": {"type": ["string", "null"]},
        # returned on the no-match branch for debuggability
        "clusters": {"type": "array"},
        # shared-dev fallback context attached by _with_integration_context
        "connection": {"type": "object"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "integration": {"type": "object"},
    },
}

# --- argocd_connection_health ---------------------------------------------
# Upstream Argo CD /health payload passthrough: declare the normalized fields,
# tolerate upstream additions.
ARGOCD_CONNECTION_HEALTH_DATA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["healthy"],
    "properties": {
        "healthy": {"type": "boolean"},
        "status": {"type": ["string", "null"]},
        "server_url": {"type": ["string", "null"]},
        "argocd_version": {"type": ["string", "null"]},
        "application_count": {"type": ["integer", "null"]},
        "checked_at": _ISO_DATETIME,
        "source": {"type": ["object", "string", "null"]},
        "truncated": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

# --- public_knowledge_search ----------------------------------------------
# Platform-api search payload passthrough: declare known result containers,
# tolerate additions (counts, pagination, scoring).
PUBLIC_KNOWLEDGE_SEARCH_DATA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "results": {"type": "array"},
        "items": {"type": "array"},
        "publicResults": {"type": "array"},
        "workspaceResults": {"type": "array"},
        "total": {"type": ["integer", "null"]},
        "query": {"type": ["string", "null"]},
    },
}

# --- external_status_check -------------------------------------------------
# Polymorphic async tool: an in-flight async ack, a completed job envelope, or a
# compacted check result. `check_status` carries the provider-check outcome
# (renamed from the ambiguous `execution_status`); the envelope-level `status`
# remains the call outcome. Declares every known field across the variants while
# tolerating upstream additions so a live async response never fails validation.
EXTERNAL_STATUS_CHECK_DATA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        # async ack variant
        "mode": {"type": ["string", "null"]},
        "job_id": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "poll_after_seconds": {"type": ["integer", "null"]},
        # compacted / completed variant
        "check_status": {"type": ["string", "null"]},
        "checked_at": _ISO_DATETIME,
        "providers": {"type": "array"},
        "errors": {"type": "array"},
        "result": {"type": ["object", "null"]},
        "response_mode": {"type": ["string", "null"]},
    },
}


# Registry: tool name -> strict data schema. Tools absent here fall back to the
# permissive (but still enveloped) generic data schema in contracts.py.
TOOL_DATA_SCHEMAS: dict[str, dict[str, Any]] = {
    "mcp_version": MCP_VERSION_DATA,
    "k8s_agent_status": K8S_AGENT_STATUS_DATA,
    "argocd_connection_health": ARGOCD_CONNECTION_HEALTH_DATA,
    "public_knowledge_search": PUBLIC_KNOWLEDGE_SEARCH_DATA,
    "external_status_check": EXTERNAL_STATUS_CHECK_DATA,
}
