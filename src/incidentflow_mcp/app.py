"""
FastAPI application factory — thin composition layer.

Wires together auth, middleware, ops routes, exception handlers, and the MCP
ASGI proxy route.  Implementation details live in the http/ subpackage.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from starlette.types import ASGIApp

from incidentflow_mcp.auth.middleware import BearerAuthMiddleware
from incidentflow_mcp.config import get_settings
from incidentflow_mcp.http.exception_handlers import register_exception_handlers
from incidentflow_mcp.http.middleware.request_id import RequestIDMiddleware
from incidentflow_mcp.http.routers.ops import create_ops_router
from incidentflow_mcp.http.routes.mcp_proxy import register_mcp_proxy_route
from incidentflow_mcp.logging_config import configure_logging
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.observability.middleware import MCPObservabilityMiddleware
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.middleware import TransportRateLimitMiddleware
from incidentflow_mcp.rate_limit.policy import DefaultPolicyResolver
from incidentflow_mcp.rate_limit.redis_store import RedisRateLimitStore
from incidentflow_mcp.rate_limit.tool_guard import ToolInvocationGuard

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """
    Application factory.

    Returns a fully configured FastAPI instance. Import and call this
    in tests or the CLI runner — never instantiate FastAPI directly.
    """
    settings = get_settings()

    if settings.environment == "production" and settings.incidentflow_pat is None:
        raise RuntimeError(
            "INCIDENTFLOW_PAT must be set in production. "
            "Set it via the INCIDENTFLOW_PAT environment variable or .env file."
        )

    # Create the MCP server once so both the lifespan and the route handler
    # share the same session_manager instance.
    mcp_server = create_mcp_server()
    mcp_http_app: ASGIApp = mcp_server.streamable_http_app()
    rate_limit_store = RedisRateLimitStore(settings.redis_url)
    rate_limit_policy = DefaultPolicyResolver(settings)
    rate_limit_bucket_keys = BucketKeyResolver()
    app_tool_guard = ToolInvocationGuard(rate_limit_store, rate_limit_policy, rate_limit_bucket_keys)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        configure_logging(settings.log_level)

        try:
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

        except Exception:
            logger.exception("application lifespan failed")
            raise
        finally:
            await rate_limit_store.close()
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

    app.state.settings = settings
    app.state.mcp_server = mcp_server
    app.state.mcp_http_app = mcp_http_app
    app.state.rate_limit_store = rate_limit_store
    app.state.rate_limit_policy = rate_limit_policy
    app.state.rate_limit_bucket_keys = rate_limit_bucket_keys
    app.state.tool_guard = app_tool_guard

    # Middleware stack (outermost → innermost; add_middleware prepends).
    app.add_middleware(TransportRateLimitMiddleware, settings=settings)
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(MCPObservabilityMiddleware, settings=settings)
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)
    app.include_router(create_ops_router(settings))
    register_mcp_proxy_route(routes=app.router.routes, path="/mcp", app=mcp_http_app)

    return app
