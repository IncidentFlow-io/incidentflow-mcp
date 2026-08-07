#!/usr/bin/env python3
"""Dump the full JSON Schema catalog for every MCP tool.

Prints every published schema (generic envelope, error object, and each per-tool
response) in full, so the complete contract is visible in CI logs — the
equivalent of ``curl /schemas`` + ``curl /schemas/{id}`` for all ids at once.

When ``GITHUB_STEP_SUMMARY`` is set (GitHub Actions), it also writes a collapsible
Markdown report to the run summary: a catalog table plus one ``<details>`` block
per schema holding the full pretty-printed JSON. Runs in-process — no server,
no network — so it is CI-friendly.

Usage:
  python scripts/dump_tool_schemas.py           # print full catalog to stdout
"""

from __future__ import annotations

import json
import os
from typing import Any


def _data_required(schema: dict[str, Any]) -> list[str]:
    """Return the ``data`` object's required fields, unwrapping a nullable anyOf."""
    data = (schema.get("properties") or {}).get("data") or {}
    if "required" in data:
        return list(data["required"])
    for branch in data.get("anyOf", []):
        if isinstance(branch, dict) and branch.get("type") == "object":
            return list(branch.get("required", []))
    return []


def build_catalog() -> tuple[list[dict[str, Any]], str, str]:
    """Return (rows, api_version, schema_version) for every published schema."""
    from incidentflow_mcp.tools import contracts
    from incidentflow_mcp.tools.output_models import schema_mode_for

    id_to_tool = {contracts.response_schema_id(t): t for t in contracts.TOOL_OUTPUT_MODELS}

    rows: list[dict[str, Any]] = []
    for sid in contracts.all_schema_ids():
        schema = contracts.get_schema(sid) or {}
        if sid == contracts.ENVELOPE_SCHEMA_ID:
            kind = "envelope (generic)"
        elif sid == contracts.ERROR_SCHEMA_ID:
            kind = "error object"
        elif sid in id_to_tool:
            kind = f"tool ({schema_mode_for(id_to_tool[sid])})"
        else:
            kind = "other"
        rows.append(
            {
                "schema_id": sid,
                "kind": kind,
                "data_required": _data_required(schema),
                "schema": schema,
            }
        )
    return rows, contracts.API_VERSION, contracts.SCHEMA_VERSION


def _stdout_report(rows: list[dict[str, Any]], api_version: str, schema_version: str) -> None:
    print(
        f"MCP tool schema catalog — {len(rows)} schemas "
        f"(api_version={api_version}, schema_version={schema_version})\n"
    )
    width = max(len(r["schema_id"]) for r in rows)
    print(f"  {'schema_id':<{width}}  kind")
    print(f"  {'-' * width}  {'-' * 20}")
    for r in rows:
        print(f"  {r['schema_id']:<{width}}  {r['kind']}")
    print()
    for r in rows:
        print(f"===== {r['schema_id']} ({r['kind']}) =====")
        print(json.dumps(r["schema"], indent=2, ensure_ascii=False))
        print()


def _github_summary(rows: list[dict[str, Any]], api_version: str, schema_version: str) -> str:
    lines: list[str] = []
    lines.append("## MCP tool schema catalog\n")
    lines.append(
        f"**{len(rows)} schemas** · api_version `{api_version}` · "
        f"schema_version `{schema_version}`\n"
    )
    lines.append("| schema_id | kind | data.required |")
    lines.append("|---|---|---|")
    for r in rows:
        req = ", ".join(f"`{k}`" for k in r["data_required"]) or "—"
        lines.append(f"| `{r['schema_id']}` | {r['kind']} | {req} |")
    lines.append("")
    for r in rows:
        body = json.dumps(r["schema"], indent=2, ensure_ascii=False)
        lines.append(f"<details><summary><code>{r['schema_id']}</code> — {r['kind']}</summary>\n")
        lines.append("```json")
        lines.append(body)
        lines.append("```")
        lines.append("</details>\n")
    return "\n".join(lines) + "\n"


def main() -> int:
    os.environ.setdefault("MCP_DEFAULT_WORKSPACE_ID", "ws_contract")
    os.environ.setdefault("ENVIRONMENT", "development")

    rows, api_version, schema_version = build_catalog()
    _stdout_report(rows, api_version, schema_version)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(_github_summary(rows, api_version, schema_version))
        print(f"Wrote schema catalog to GitHub step summary ({len(rows)} schemas).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
