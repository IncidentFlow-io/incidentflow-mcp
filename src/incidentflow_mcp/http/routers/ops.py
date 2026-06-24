"""
Ops router — liveness and readiness probes.

Use create_ops_router(settings) to get a fully configured APIRouter that can
be included into the FastAPI app via app.include_router(...).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.http.install_script import render_install_script
from incidentflow_mcp.observability.metrics import METRICS_CONTENT_TYPE, render_prometheus_metrics


def create_ops_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["ops"])

    @router.get("/install.sh", summary="Installer script")
    async def install_sh(request: Request) -> Response:
        """Return a curl-able installer script with a URL derived from current host."""
        body = render_install_script(request)
        return Response(
            content=body,
            media_type="text/x-shellscript; charset=utf-8",
            headers={
                "Content-Disposition": 'inline; filename="install.sh"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/healthz", summary="Liveness probe")
    async def healthz() -> JSONResponse:
        """Returns 200 OK. Used by Docker/Kubernetes liveness probes — no auth required."""
        return JSONResponse(
            content={
                "status": "ok",
                "service": settings.mcp_server_name,
                "version": settings.mcp_server_version,
                "environment": settings.environment,
            }
        )

    @router.get("/readyz", summary="Readiness probe")
    async def readyz() -> JSONResponse:
        """Returns 200 when the app is ready to serve traffic — no auth required."""
        return JSONResponse(content={"status": "ready"})

    @router.get("/metrics", summary="Prometheus metrics")
    async def metrics() -> Response:
        """Prometheus metrics endpoint."""
        payload = render_prometheus_metrics()
        return Response(content=payload, media_type=METRICS_CONTENT_TYPE)

    @router.get(
        "/.well-known/oauth-protected-resource",
        summary="OAuth protected resource metadata",
    )
    async def oauth_protected_resource(request: Request) -> JSONResponse:
        _ = request
        auth_server = (
            settings.oauth_expected_issuer
            or settings.platform_api_base_url
            or str(request.base_url).rstrip("/")
        )
        return JSONResponse(
            content={
                "resource": settings.mcp_canonical_resource,
                "authorization_servers": [auth_server],
                "scopes_supported": ["mcp:read", "mcp:tools:run", "admin"],
            }
        )

    @router.get(
        "/.well-known/oauth-protected-resource/mcp",
        summary="OAuth protected resource metadata (MCP path)",
    )
    async def oauth_protected_resource_mcp(request: Request) -> JSONResponse:
        _ = request
        auth_server = (
            settings.oauth_expected_issuer
            or settings.platform_api_base_url
            or str(request.base_url).rstrip("/")
        )
        return JSONResponse(
            content={
                "resource": settings.mcp_canonical_resource,
                "authorization_servers": [auth_server],
                "scopes_supported": ["mcp:read", "mcp:tools:run", "admin"],
            }
        )

    @router.get("/.well-known/{challenge_path:path}", summary="OpenAI domain verification")
    async def openai_domain_verification(challenge_path: str) -> Response:
        """Return the configured OpenAI Apps domain-verification token."""
        configured_path = settings.openai_domain_verification_path
        token = settings.openai_domain_verification_token
        request_path = f"/.well-known/{challenge_path}"
        if (
            not configured_path
            or not token
            or not configured_path.startswith("/.well-known/")
            or request_path != configured_path
        ):
            raise HTTPException(status_code=404, detail="Not found")

        return Response(
            content=token.get_secret_value(),
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    return router
