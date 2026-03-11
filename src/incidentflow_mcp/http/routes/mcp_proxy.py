"""
Direct ASGI proxy route for the MCP endpoint.

Registers an exact-path route that forwards requests to the FastMCP sub-app
with the original ASGI scope unchanged.  Using Starlette's Mount would strip
the path prefix, leaving scope["path"] == "" for a request to "/mcp", which
breaks FastMCP's internal routing.  This avoids that and has no dependency on
private Starlette internals.
"""

from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import ASGIApp, Receive, Scope, Send

# Methods accepted on the MCP endpoint.
# Only GET/POST are required by the MCP Streamable HTTP spec;
# OPTIONS is kept for CORS preflight compatibility.
_MCP_METHODS: frozenset[str] = frozenset({"GET", "POST", "OPTIONS"})


class MCPASGIProxyRoute(BaseRoute):
    """
    Exact-path ASGI proxy route.

    Matches only the configured path and the allowed HTTP methods, then
    delivers the full original scope to the downstream ASGI app unchanged.
    """

    def __init__(self, path: str, app: ASGIApp) -> None:
        self._path = path
        self._app = app

    def matches(self, scope: Scope) -> tuple[Match, dict]:  # type: ignore[override]
        if scope.get("type") == "http" and scope.get("path") == self._path:
            if scope.get("method", "").upper() in _MCP_METHODS:
                return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: object) -> object:  # type: ignore[override]
        raise NoMatchFound(name, path_params)


def register_mcp_proxy_route(*, routes: list, path: str, app: ASGIApp) -> None:
    """Append an MCPASGIProxyRoute to an existing route list (e.g. app.router.routes)."""
    routes.append(MCPASGIProxyRoute(path, app))
