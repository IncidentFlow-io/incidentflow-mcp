"""
CLI entrypoint for running the IncidentFlow MCP server locally.

Usage:
    uv run incidentflow-mcp serve
    uv run incidentflow-mcp serve --host 127.0.0.1 --port 8000
    uv run incidentflow-mcp token create --name "local-dev"
    uv run incidentflow-mcp token create --name "ci" --scopes mcp:read,mcp:tools:run --expires-in-days 30
    uv run incidentflow-mcp token list
    uv run incidentflow-mcp token revoke <token_id>
    uv run incidentflow-mcp tools list
    uv run incidentflow-mcp tools list --verbose
    uv run incidentflow-mcp tools list --json-output
    uv run incidentflow-mcp tools show incident_summary
    uv run incidentflow-mcp openapi export --output openapi.json
    uv run incidentflow-mcp --help
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import click
import uvicorn

from incidentflow_mcp.config import get_settings
from incidentflow_mcp.logging_config import configure_logging

logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """IncidentFlow MCP — local development server."""


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@cli.command("serve")
@click.option("--host", default=None, help="Bind host (overrides HOST env var)")
@click.option("--port", default=None, type=int, help="Bind port (overrides PORT env var)")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (dev only)")
@click.option("--log-level", default=None, help="Log level: debug|info|warning|error")
def serve(
    host: str | None,
    port: int | None,
    reload: bool,
    log_level: str | None,
) -> None:
    """Start the MCP HTTP server."""
    settings = get_settings()

    _host = host or settings.host
    _port = port or settings.port
    _level = log_level or settings.log_level

    configure_logging(_level)
    logger.info("launching server on %s:%d", _host, _port)

    uvicorn.run(
        "incidentflow_mcp.app:create_app",
        host=_host,
        port=_port,
        factory=True,
        log_level=_level.lower(),
        reload=reload,
    )


# ---------------------------------------------------------------------------
# token — PAT management commands
# ---------------------------------------------------------------------------


@cli.group("token")
def token_group() -> None:
    """Manage Personal Access Tokens."""


@token_group.command("create")
@click.option("--name", required=True, help="Human-readable name for the token")
@click.option(
    "--scopes",
    default="mcp:read,mcp:tools:run",
    show_default=True,
    help="Comma-separated list of scopes",
)
@click.option(
    "--expires-in-days",
    default=None,
    type=int,
    metavar="DAYS",
    help="Expire the token after N days (default: never)",
)
def token_create(name: str, scopes: str, expires_in_days: int | None) -> None:
    """Generate a new Personal Access Token and store it in the token DB."""
    from incidentflow_mcp.auth.repository import JsonTokenRepository, TokenRecord
    from incidentflow_mcp.auth.tokens import generate_pat

    plaintext, token_id, token_hash = generate_pat()
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=expires_in_days) if expires_in_days else None

    record = TokenRecord(
        token_id=token_id,
        token_hash=token_hash,
        name=name,
        scopes=scope_list,
        created_at=now,
        expires_at=expires_at,
    )

    repo = JsonTokenRepository()
    repo.save(record)

    click.echo("\nToken created successfully!\n")
    click.echo(f"  Name:     {name}")
    click.echo(f"  Token ID: {token_id}")
    click.echo(f"  Scopes:   {', '.join(scope_list)}")
    if expires_at:
        click.echo(f"  Expires:  {expires_at.strftime('%Y-%m-%d %H:%M UTC')}")
    click.echo("\n  Token (shown once — store it securely):\n")
    click.echo(f"    {plaintext}\n")
    click.echo("  Set it in your environment:")
    click.echo(f"    export INCIDENTFLOW_PAT={plaintext}\n")


@token_group.command("list")
def token_list() -> None:
    """List all Personal Access Tokens."""
    from incidentflow_mcp.auth.repository import JsonTokenRepository

    repo = JsonTokenRepository()
    records = repo.list_all()

    if not records:
        click.echo("No tokens found.")
        return

    header = f"{'ID':<12}  {'NAME':<24}  {'SCOPES':<28}  {'CREATED':<20}  STATUS"
    click.echo(f"\n{header}")
    click.echo("-" * len(header))

    now = datetime.now(timezone.utc)
    for r in sorted(records, key=lambda x: x.created_at):
        if r.revoked_at:
            status = "revoked"
        elif r.expires_at and r.expires_at < now:
            status = "expired"
        else:
            status = "active"
        scopes_str = ",".join(r.scopes)
        created_str = r.created_at.strftime("%Y-%m-%d %H:%M")
        click.echo(f"{r.token_id:<12}  {r.name:<24}  {scopes_str:<28}  {created_str:<20}  {status}")

    click.echo()


@token_group.command("revoke")
@click.argument("token_id")
def token_revoke(token_id: str) -> None:
    """Revoke a Personal Access Token by TOKEN_ID."""
    from incidentflow_mcp.auth.repository import JsonTokenRepository

    repo = JsonTokenRepository()
    try:
        repo.revoke(token_id, datetime.now(timezone.utc))
        click.echo(f"Token {token_id!r} has been revoked.")
    except KeyError:
        click.echo(f"Error: token {token_id!r} not found.", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# tools — MCP tool inspection
# ---------------------------------------------------------------------------


@cli.group("tools")
def tools_group() -> None:
    """Inspect registered MCP tools."""


@tools_group.command("list")
@click.option("--verbose", is_flag=True, default=False, help="Show full input schema and annotations")
@click.option("--json-output", is_flag=True, default=False, help="Print raw JSON (machine-readable)")
def tools_list(verbose: bool, json_output: bool) -> None:
    """List all registered MCP tools."""
    from incidentflow_mcp.tools.registry import get_tool_specs

    tools = get_tool_specs()

    if not tools:
        click.echo("No tools registered.")
        return

    if json_output:
        click.echo(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                        "annotations": t.annotations,
                    }
                    for t in tools
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not verbose:
        click.echo("\nRegistered MCP tools\n")
        header = f"{'NAME':<24}  DESCRIPTION"
        click.echo(header)
        click.echo("-" * 80)
        for t in tools:
            desc = t.description.strip().replace("\n", " ")
            if len(desc) > 56:
                desc = desc[:53] + "..."
            click.echo(f"{t.name:<24}  {desc}")
        click.echo()
        click.echo("Descriptions are truncated. Use --verbose for full details.")
        click.echo(f"{len(tools)} tool{'s' if len(tools) != 1 else ''} registered.")
        click.echo()
        return

    for t in tools:
        click.echo(f"\n{t.name}")
        click.echo(f"  Description: {t.description}")

        props = t.input_schema.get("properties", {})
        required = set(t.input_schema.get("required", []))
        if props:
            click.echo("  Input schema:")
            for field_name, field_schema in props.items():
                field_type = field_schema.get("type", "any")
                req_label = "required" if field_name in required else "optional"
                desc = field_schema.get("description", "")
                suffix = f"  # {desc}" if desc else ""
                click.echo(f"    - {field_name} ({req_label}, {field_type}){suffix}")
        else:
            click.echo("  Input schema: none")

        if t.annotations:
            click.echo("  Annotations:")
            for k, v in t.annotations.items():
                click.echo(f"    - {k}: {v}")
    click.echo()


@tools_group.command("show")
@click.argument("tool_name")
def tools_show(tool_name: str) -> None:
    """Show full details for a single MCP tool by name."""
    from incidentflow_mcp.tools.registry import get_tool_specs

    specs = {t.name: t for t in get_tool_specs()}
    t = specs.get(tool_name)

    if t is None:
        available = ", ".join(specs) or "none"
        click.echo(f"Error: tool {tool_name!r} not found. Available: {available}", err=True)
        raise SystemExit(1)

    click.echo(f"\n{t.name}")
    click.echo(f"  Description: {t.description}")

    props = t.input_schema.get("properties", {})
    required = set(t.input_schema.get("required", []))
    if props:
        click.echo("  Input schema:")
        for field_name, field_schema in props.items():
            field_type = field_schema.get("type", "any")
            req_label = "required" if field_name in required else "optional"
            desc = field_schema.get("description", "")
            suffix = f"  # {desc}" if desc else ""
            click.echo(f"    - {field_name} ({req_label}, {field_type}){suffix}")
    else:
        click.echo("  Input schema: none")

    if t.annotations:
        click.echo("  Annotations:")
        for k, v in t.annotations.items():
            click.echo(f"    - {k}: {v}")
    click.echo()


# ---------------------------------------------------------------------------
# openapi — schema export
# ---------------------------------------------------------------------------


@cli.group("openapi")
def openapi_group() -> None:
    """Generate and export OpenAPI schema."""


@openapi_group.command("export")
@click.option(
    "--output",
    default="openapi.json",
    show_default=True,
    help="Destination path for generated OpenAPI schema",
)
@click.option(
    "--server-url",
    default=None,
    help="Optional server URL to set in schema (e.g. https://api.example.com)",
)
def openapi_export(output: str, server_url: str | None) -> None:
    """Export OpenAPI JSON from the current FastAPI app configuration."""
    from incidentflow_mcp.app import create_app

    app = create_app()
    schema = app.openapi()

    # Auth is enforced by middleware, so annotate it explicitly in OpenAPI.
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "PAT",
    }

    mcp_path = schema.get("paths", {}).get("/mcp", {})
    for method in ("get", "post"):
        operation = mcp_path.get(method)
        if isinstance(operation, dict):
            operation["security"] = [{"bearerAuth": []}]

    # FastAPI cannot infer a body schema for proxy-style Request handlers,
    # so we enrich MCP POST manually for docs/playground usability.
    post_op = mcp_path.get("post")
    if isinstance(post_op, dict):
        post_op["parameters"] = [
            {
                "name": "Accept",
                "in": "header",
                "required": True,
                "description": "MCP streamable HTTP requires both media types.",
                "schema": {
                    "type": "string",
                    "default": "application/json, text/event-stream",
                },
            },
            {
                "name": "MCP-Protocol-Version",
                "in": "header",
                "required": False,
                "schema": {"type": "string", "default": "2025-03-26"},
            },
        ]
        post_op["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object", "additionalProperties": True},
                    "examples": {
                        "initialize": {
                            "summary": "MCP initialize request",
                            "value": {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2025-03-26",
                                    "clientInfo": {"name": "mintlify-playground", "version": "1.0.0"},
                                    "capabilities": {}
                                }
                            }
                        }
                    },
                }
            },
        }
        post_op["responses"] = {
            "200": {
                "description": "MCP stream response",
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"}
                    }
                },
            },
            "400": {"description": "Invalid request (e.g. missing/invalid Content-Type)"},
            "406": {"description": "Missing required Accept header"},
        }

    if server_url:
        schema["servers"] = [{"url": server_url, "description": "Configured by CLI export"}]

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    click.echo(f"OpenAPI exported to: {output_path}")
