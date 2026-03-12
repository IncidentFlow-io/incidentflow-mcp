"""Dynamic installer script rendering for /install.sh."""

from importlib.resources import files

from fastapi import Request

_INSTALL_TEMPLATE = "install.sh.template"


def build_server_origin(request: Request) -> str:
    """Build canonical public origin using forwarded headers when present."""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")

    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def render_install_script(request: Request) -> str:
    """Read installer template and substitute public origin and MCP URL."""
    origin = build_server_origin(request)
    mcp_url = f"{origin}/mcp"

    template = (
        files("incidentflow_mcp.assets")
        .joinpath(_INSTALL_TEMPLATE)
        .read_text(encoding="utf-8")
    )
    return template.replace("{{SERVER_ORIGIN}}", origin).replace("{{MCP_URL}}", mcp_url)
