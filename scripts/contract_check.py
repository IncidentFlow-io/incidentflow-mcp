#!/usr/bin/env python3
"""Three-level contract verification for the IncidentFlow MCP server.

Levels (as the review requested):
  1. the service publishes a version/contract block;
  2. the service publishes JSON Schemas (envelope, error, per-tool);
  3. real tool responses validate against the published schemas — with a
     date-time format checker.

Default mode is **in-process** (no network / no OAuth): it builds the server,
inspects every tool's published ``outputSchema``, then calls the read-only
fixture manifest through the tool manager and validates each response envelope.
This makes it CI-friendly and importable by pytest.

Usage:
  python scripts/contract_check.py                # in-process (clean report)
  python scripts/contract_check.py --verbose      # keep httpx / app logs
  python scripts/contract_check.py --list         # print catalog + coverage
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "contract_calls.json"

ENVELOPE_KEYS = {
    "api_version",
    "schema_version",
    "schema_id",
    "status",
    "request_id",
    "data",
    "error",
    "meta",
}


def _auth_context() -> dict[str, Any]:
    return {
        "authenticated": True,
        "auth_method": "oauth",
        "bearer_token": "contract-check-token",
        "client_id": "contract-check",
        "workspace_id": "ws_contract",
        "workspace_name": "Contract Check",
        "workspace_slug": "contract",
        "workspace_role": "owner",
        "user_id": "user_contract",
        "email": "contract@example.com",
        "plan": None,
    }


def _quiet_noise() -> None:
    """Silence per-request httpx logs and app integration warnings.

    Level 3 deliberately exercises downstream calls that fail (no live
    integrations / no real auth). The raw ``HTTP Request: ... 401`` lines and
    ``integration_status_request_failed`` warnings interleave with the report and
    obscure it — the *contract* result is surfaced per tool instead. Use
    ``--verbose`` to keep them.
    """

    for noisy in ("httpx", "httpcore", "incidentflow_mcp"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


async def run_in_process(*, verbose: bool = False) -> int:
    """Run the three-level check in-process. Returns a process exit code."""

    import os

    os.environ.setdefault("MCP_DEFAULT_WORKSPACE_ID", "ws_contract")
    os.environ.setdefault("ENVIRONMENT", "development")

    from incidentflow_mcp.auth.context import (
        clear_current_auth_context,
        set_current_auth_context,
    )
    from incidentflow_mcp.mcp.server import create_mcp_server
    from incidentflow_mcp.tools import contracts

    failures: list[str] = []

    print("IncidentFlow MCP — contract verification (in-process)")
    print("Proves the response CONTRACT (shape), not integration health:")
    print("  * the service advertises api_version / schema_version / contract_version")
    print("  * every tool publishes a Draft 2020-12 input + output schema (8-key envelope)")
    print("  * real responses — success AND error — validate against those schemas\n")

    # Level 1 — version / contract block.
    print("Level 1 — version / contract block")
    print(
        f"  api_version = {contracts.API_VERSION}   "
        f"schema_version = {contracts.SCHEMA_VERSION}   "
        f"contract_version = {contracts.CONTRACT_VERSION}"
    )
    if contracts.API_VERSION != "v1":
        failures.append("api_version is not v1")

    # Level 2 — published JSON Schemas.
    print("\nLevel 2 — published JSON Schemas")
    schema_ids = contracts.all_schema_ids()
    for sid in schema_ids:
        schema = contracts.get_schema(sid)
        if schema is None:
            failures.append(f"catalog lists {sid} but get_schema returned None")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            failures.append(f"invalid schema {sid}: {exc}")
    print(f"  {len(schema_ids)} schemas published — all valid Draft 2020-12")

    mcp = create_mcp_server()
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}

    # Every tool publishes an outputSchema with the 8 envelope keys required.
    for name, tool in tools.items():
        out = tool.fn_metadata.output_schema
        if out is None:
            failures.append(f"{name}: no outputSchema published")
            continue
        required = set(out.get("required", []))
        if not ENVELOPE_KEYS.issubset(required):
            failures.append(
                f"{name}: outputSchema missing envelope keys {ENVELOPE_KEYS - required}"
            )
        if tool.fn_metadata.arg_model is None:
            failures.append(f"{name}: no inputSchema")
    print(f"  {len(tools)} tools publish input + output schemas with the 8 envelope keys")

    # Level 3 — real responses validate against published schemas.
    print("\nLevel 3 — real responses validate against their schema")
    print(
        "  (an error envelope is EXPECTED here without live integrations / auth;\n"
        "   a valid error envelope with a canonical code is a PASS, not a failure)\n"
    )
    if not verbose:
        _quiet_noise()

    print(f"  {'tool':<28} {'result':<8} {'code':<24} note")
    print(f"  {'-' * 28} {'-' * 8} {'-' * 24} {'-' * 24}")

    manifest = json.loads(MANIFEST_PATH.read_text())
    set_current_auth_context(_auth_context())
    n_success = n_error = 0
    try:
        for call in manifest["calls"]:
            name = call["name"]
            tool = tools.get(name)
            if tool is None:
                failures.append(f"manifest tool not registered: {name}")
                continue
            result = await mcp._tool_manager.call_tool(name, call.get("arguments", {}))
            envelope = result.structuredContent if hasattr(result, "structuredContent") else result
            if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
                failures.append(f"{name}: response is not a canonical envelope")
                print(f"  {name:<28} {'BROKEN':<8} {'-':<24} not an 8-key envelope")
                continue
            schema = contracts.build_output_schema(name)
            errors = sorted(
                Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(envelope),
                key=str,
            )
            status = str(envelope.get("status"))
            error_obj = envelope.get("error") or {}
            code = str(error_obj.get("code") or "-") if status == "error" else "-"
            if errors:
                failures.append(f"{name}: response fails schema: {errors[0].message}")
                print(f"  {name:<28} {'FAIL':<8} {code:<24} {errors[0].message[:60]}")
                continue
            if status == "success":
                n_success += 1
                note = "envelope + data valid"
            else:
                n_error += 1
                note = str(error_obj.get("message") or "")[:44]
            print(f"  {name:<28} {status:<8} {code:<24} {note}")
    finally:
        clear_current_auth_context()

    checked = n_success + n_error
    print(
        f"\n  -> {checked} tools checked * {n_success} success * {n_error} error"
        f" * {len(failures)} schema violation(s)"
    )

    print()
    if failures:
        print(f"FAILED ({len(failures)} problem(s)) — a response did NOT match its schema:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — all three levels passed. The response contract is valid.")
    return 0


def print_catalog() -> int:
    from incidentflow_mcp.tools import contracts
    from incidentflow_mcp.tools.output_models import schema_mode_for

    ids = contracts.all_schema_ids()
    strict = [n for n in contracts.TOOL_OUTPUT_MODELS if schema_mode_for(n) == "strict"]
    print(json.dumps({"schema_count": len(ids), "strict_tools": sorted(strict)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IncidentFlow MCP contract verification")
    parser.add_argument("--list", action="store_true", help="print schema catalog + coverage")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="keep httpx / app logs (default: quiet for a clean report)",
    )
    args = parser.parse_args()
    if args.list:
        return print_catalog()
    return asyncio.run(run_in_process(verbose=args.verbose))


if __name__ == "__main__":
    sys.exit(main())
