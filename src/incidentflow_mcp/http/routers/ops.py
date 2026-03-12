"""
Ops router — liveness and readiness probes.

Use create_ops_router(settings) to get a fully configured APIRouter that can
be included into the FastAPI app via app.include_router(...).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.http.install_script import render_install_script


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

    return router
