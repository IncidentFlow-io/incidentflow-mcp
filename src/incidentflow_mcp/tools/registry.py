"""
Single source of truth for all MCP tool metadata.

Both the MCP server (mcp/server.py) and the CLI (cli/main.py) read from this
module so that tool names, descriptions, input schemas, and annotations are
never duplicated.

To add a new tool:
  1. Append a ToolSpec entry to _TOOL_SPECS.
  2. Register the implementation in mcp/server.py using the same name.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="incident_summary",
        description=(
            "Return a structured summary for a given incident, including title, "
            "severity, status, affected services, event timeline, and remediation "
            "recommendations."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "Unique incident identifier (e.g. INC-001)",
                },
                "include_timeline": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include event timeline in the response",
                },
                "include_affected_services": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include impacted service list in the response",
                },
            },
            "required": ["incident_id"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    ),
    ToolSpec(
        name="correlate_alerts",
        description=(
            "Group a list of incoming alerts into correlated clusters based on "
            "shared service, label affinity, and time proximity. Returns cluster "
            "assignments, dominant severity, likely root cause, and confidence score."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "alerts_json": {
                    "type": "string",
                    "description": "JSON array of Alert objects to correlate",
                },
                "window_minutes": {
                    "type": "integer",
                    "default": 60,
                    "description": "Correlation time window in minutes (1–1440)",
                },
                "min_cluster_size": {
                    "type": "integer",
                    "default": 2,
                    "description": "Minimum number of alerts required to form a cluster",
                },
            },
            "required": ["alerts_json"],
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    ),
]


def get_tool_specs() -> list[ToolSpec]:
    """Return all registered tool specifications."""
    return list(_TOOL_SPECS)
