"""Generate JSON Schemas for IncidentFlow MCP tool response contracts."""

from __future__ import annotations

from pathlib import Path

from incidentflow_mcp.tools.contracts import export_tool_schemas
from incidentflow_mcp.tools.registry import get_tool_specs


def main() -> None:
    output_dir = Path(__file__).resolve().parents[1] / "schemas" / "tools"
    written = export_tool_schemas(get_tool_specs(), output_dir)
    print(f"Wrote {len(written)} schema files to {output_dir}")


if __name__ == "__main__":
    main()
