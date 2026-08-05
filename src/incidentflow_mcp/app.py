"""
FastAPI application factory — thin composition layer.

Wires together auth, middleware, ops routes, exception handlers, and the MCP
ASGI proxy route.  Implementation details live in the http/ subpackage.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp

from incidentflow_mcp.auth.middleware import BearerAuthMiddleware
from incidentflow_mcp.config import Settings, get_settings
from incidentflow_mcp.http.exception_handlers import register_exception_handlers
from incidentflow_mcp.http.middleware.request_id import RequestIDMiddleware
from incidentflow_mcp.http.routers.ops import create_ops_router
from incidentflow_mcp.http.routes.mcp_proxy import register_mcp_proxy_route
from incidentflow_mcp.logging_config import configure_logging
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.observability.middleware import MCPObservabilityMiddleware
from incidentflow_mcp.observability.tracing import configure_tracing
from incidentflow_mcp.rate_limit.bucket_keys import BucketKeyResolver
from incidentflow_mcp.rate_limit.middleware import TransportRateLimitMiddleware
from incidentflow_mcp.rate_limit.policy import DefaultPolicyResolver
from incidentflow_mcp.rate_limit.redis_store import RedisRateLimitStore
from incidentflow_mcp.rate_limit.tool_guard import ToolInvocationGuard

logger = logging.getLogger(__name__)


def _auth_mode_label(settings: Settings) -> str:
    if settings.oauth_validation_enabled():
        return "oauth_jwt"
    if settings.managed_token_introspection_enabled():
        return "managed_token_introspection"
    if settings.incidentflow_pat is not None:
        return "static_pat"
    return "unprotected"


def create_app() -> FastAPI:
    """
    Application factory.

    Returns a fully configured FastAPI instance. Import and call this
    in tests or the CLI runner — never instantiate FastAPI directly.
    """
    settings = get_settings()
    configure_logging(
        settings.log_level,
        settings.library_log_level,
        settings.log_format,
        service=settings.mcp_server_name,
        service_version=settings.mcp_server_version,
        environment=settings.runtime_environment(),
    )

    if (
        settings.environment == "production"
        and settings.incidentflow_pat is None
        and not settings.oauth_validation_enabled()
        and not settings.managed_token_introspection_enabled()
        and not settings.allow_unprotected_in_production
    ):
        raise RuntimeError(
            "Auth must be configured in production. "
            "Set INCIDENTFLOW_PAT or PLATFORM_API_BASE_URL. "
            "To bypass temporarily, set ALLOW_UNPROTECTED_IN_PRODUCTION=true."
        )

    if settings.runtime_environment() == "production" and settings.shared_dev_kubernetes_enabled:
        raise RuntimeError(
            "Shared development Kubernetes fallback cannot be enabled in production."
        )

    # Create the MCP server once so both the lifespan and the route handler
    # share the same session_manager instance.
    mcp_server = create_mcp_server()
    mcp_http_app: ASGIApp = mcp_server.streamable_http_app()
    rate_limit_store = RedisRateLimitStore(settings.redis_url)
    rate_limit_policy = DefaultPolicyResolver(settings)
    rate_limit_bucket_keys = BucketKeyResolver()
    app_tool_guard = ToolInvocationGuard(
        rate_limit_store,
        rate_limit_policy,
        rate_limit_bucket_keys,
    )

    # configure_tracing must run before FastAPI is instantiated so that
    # FastAPIInstrumentor middleware is included in Starlette's middleware stack.
    configure_tracing(
        service_name=settings.mcp_server_name,
        service_version=settings.service_version,
        environment=settings.environment,
        otlp_endpoint=settings.observability_otlp_endpoint,
        k8s_namespace=settings.k8s_namespace_name,
        enabled=settings.observability_enabled and settings.observability_tracing_enabled,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        try:
            if settings.oauth_validation_enabled():
                logger.debug(
                    "auth_oauth_jwt_enabled",
                    extra={
                        "oauth_issuer": settings.oauth_expected_issuer,
                        "oauth_jwks_url": settings.oauth_jwks_url,
                    },
                )
            if settings.managed_token_introspection_enabled():
                logger.debug(
                    "auth_token_introspection_enabled",
                    extra={
                        "platform_api_base_url": settings.platform_api_base_url,
                        "platform_api_introspect_path": settings.platform_api_introspect_path,
                    },
                )
            elif settings.incidentflow_pat is None:
                logger.warning(
                    "auth_unprotected",
                    extra={
                        "log_message": ("No auth provider configured; MCP endpoint is unprotected.")
                    },
                )

            logger.info(
                "server_started",
                extra={
                    "host": settings.host,
                    "port": settings.port,
                    "auth_mode": _auth_mode_label(settings),
                    "token_introspection_enabled": (settings.managed_token_introspection_enabled()),
                    "mcp_transport": "streamable_http",
                    "mcp_stateless": True,
                },
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
            logger.info(
                "server_stopped",
                extra={"shutdown_reason": "lifespan_shutdown"},
            )

    app = FastAPI(
        title="IncidentFlow MCP",
        version=settings.mcp_server_version,
        description="HTTP-based MCP server for IncidentFlow AI-powered incident management",
        lifespan=_lifespan,
        redirect_slashes=False,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
    )

    # Instrument FastAPI after app creation but before serving starts.
    # Must happen here (not in lifespan) so Starlette includes the middleware.
    from incidentflow_mcp.observability.tracing import instrument_fastapi_app

    instrument_fastapi_app(app)

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
