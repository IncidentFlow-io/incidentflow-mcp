"""
FastAPI application factory.

Wires together:
    - BearerAuthMiddleware  (local PAT auth)
    - /healthz              (liveness probe)
    - /mcp                  (Streamable HTTP MCP transport — direct ASGI route)
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from incidentflow_mcp.auth.middleware import BearerAuthMiddleware
from incidentflow_mcp.config import get_settings
from incidentflow_mcp.logging_config import configure_logging
from incidentflow_mcp.mcp.server import create_mcp_server

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """
    Application factory.

    Returns a fully configured FastAPI instance. Import and call this
    in tests or the CLI runner — never instantiate FastAPI directly.
    """
    settings = get_settings()

    # Create the MCP server once so both the lifespan and the route handler
    # share the same session_manager instance.
    mcp_server = create_mcp_server()
    mcp_http_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        configure_logging(settings.log_level)

        if settings.incidentflow_pat is None:
            logger.warning(
                "INCIDENTFLOW_PAT is not set — MCP endpoint is UNPROTECTED. "
                "Set INCIDENTFLOW_PAT in your .env file for local dev auth."
            )
        else:
            logger.info("auth: Bearer PAT protection is active")

        logger.info(
            "starting %s v%s on %s:%d",
            settings.mcp_server_name,
            settings.mcp_server_version,
            settings.host,
            settings.port,
        )

        # Drive the StreamableHTTPSessionManager task group.
        # We enter run() ourselves because FastAPI does not forward lifespan
        # events to ASGI apps that are called directly (not via Mount).
        async with mcp_server.session_manager.run():
            yield

        logger.info("shutdown complete")

    app = FastAPI(
        title="IncidentFlow MCP",
        version=settings.mcp_server_version,
        description="HTTP-based MCP server for IncidentFlow AI-powered incident management",
        lifespan=_lifespan,
        redirect_slashes=False,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
    )

    # ------------------------------------------------------------------
    # Auth middleware — protects all paths except those in _PUBLIC_PATHS
    # ------------------------------------------------------------------
    app.add_middleware(BearerAuthMiddleware)

    # ------------------------------------------------------------------
    # Health endpoint
    # ------------------------------------------------------------------

    @app.get("/healthz", tags=["ops"], summary="Liveness probe")
    async def healthz() -> JSONResponse:
        """Returns 200 OK. Used by Docker/Kubernetes liveness probes — no auth required."""
        return JSONResponse(
            content={
                "status": "ok",
                "service": settings.mcp_server_name,
                "version": settings.mcp_server_version,
            }
        )

    # ------------------------------------------------------------------
    # MCP endpoint — forward directly to the FastMCP ASGI app.
    #
    # We do NOT use app.mount() because Starlette's Mount strips the path
    # prefix before calling the sub-app, leaving scope["path"]="" for a
    # request to exactly /mcp.  FastMCP's internal route is registered at
    # "/" which never matches "".
    #
    # Instead we register a catch-all APIRoute at /mcp that calls the
    # FastMCP ASGI app directly with the original scope untouched.
    # FastMCP (streamable_http_path="/mcp") expects scope["path"]=="/mcp"
    # and handles it correctly.
    # ------------------------------------------------------------------

    @app.api_route("/mcp", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
    async def mcp_endpoint(request: Request) -> None:
        """Proxy all /mcp requests directly to the FastMCP ASGI app."""
        await mcp_http_app(request.scope, request.receive, request._send)  # type: ignore[attr-defined]

    return app

