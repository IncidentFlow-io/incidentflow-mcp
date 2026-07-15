"""
Ops router — liveness and readiness probes.

Use create_ops_router(settings) to get a fully configured APIRouter that can
be included into the FastAPI app via app.include_router(...).
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from incidentflow_mcp.config import Settings
from incidentflow_mcp.http.install_script import render_install_script
from incidentflow_mcp.observability.metrics import METRICS_CONTENT_TYPE, render_prometheus_metrics


def _oauth_authority_base(settings: Settings, request: Request) -> str:
    return (
        settings.oauth_expected_issuer
        or settings.platform_api_base_url
        or str(request.base_url).rstrip("/")
    ).rstrip("/")


def _oauth_metadata(settings: Settings, request: Request, *, openid: bool = False) -> dict:
    auth_base = _oauth_authority_base(settings, request)
    scopes_supported = (
        ["openid", "email", "profile", "mcp:read", "mcp:tools:run"]
        if openid
        else ["mcp:read", "mcp:tools:run"]
    )
    payload = {
        "issuer": auth_base,
        "authorization_endpoint": f"{auth_base}/authorize",
        "token_endpoint": f"{auth_base}/token",
        "registration_endpoint": f"{auth_base}/register",
        "jwks_uri": settings.oauth_jwks_url or f"{auth_base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "scopes_supported": scopes_supported,
    }
    if openid:
        payload.update(
            {
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        )
    else:
        payload.update(
            {
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            }
        )
    return payload


async def _proxy_to_oauth_authority(
    request: Request,
    settings: Settings,
    path: str,
) -> Response:
    auth_base = _oauth_authority_base(settings, request)
    if auth_base == str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=404, detail="OAuth authorization server is not configured")

    target_url = f"{auth_base}{path}"
    body = await request.body()
    headers = {}
    for name in ("content-type", "accept"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    async with httpx.AsyncClient(timeout=settings.platform_api_timeout_seconds) as client:
        upstream = await client.request(
            request.method,
            target_url,
            params=request.query_params,
            content=body,
            headers=headers,
            follow_redirects=False,
        )

    response_headers = {}
    location = upstream.headers.get("location")
    if location:
        response_headers["location"] = location
    media_type = upstream.headers.get("content-type")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=media_type,
    )


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
                "scopes_supported": ["mcp:read", "mcp:tools:run"],
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
                "scopes_supported": ["mcp:read", "mcp:tools:run"],
            }
        )

    @router.get(
        "/.well-known/oauth-authorization-server",
        summary="OAuth authorization server metadata",
    )
    async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
        return JSONResponse(content=_oauth_metadata(settings, request, openid=False))

    @router.get(
        "/.well-known/openid-configuration",
        summary="OpenID Connect discovery metadata",
    )
    async def openid_configuration(request: Request) -> JSONResponse:
        return JSONResponse(content=_oauth_metadata(settings, request, openid=True))

    @router.get("/.well-known/jwks.json", summary="OAuth JWKS redirect")
    async def oauth_jwks(request: Request) -> RedirectResponse:
        jwks_uri = settings.oauth_jwks_url or (
            f"{_oauth_authority_base(settings, request)}/.well-known/jwks.json"
        )
        if jwks_uri == str(request.url):
            raise HTTPException(status_code=404, detail="OAuth JWKS is not configured")
        return RedirectResponse(url=jwks_uri, status_code=307)

    @router.post("/register", summary="OAuth dynamic client registration bridge")
    @router.post("/oauth/register", summary="OAuth dynamic client registration bridge")
    async def oauth_register(request: Request) -> Response:
        return await _proxy_to_oauth_authority(request, settings, "/register")

    @router.get("/authorize", summary="OAuth authorization redirect")
    async def oauth_authorize(request: Request) -> RedirectResponse:
        auth_base = _oauth_authority_base(settings, request)
        if auth_base == str(request.base_url).rstrip("/"):
            raise HTTPException(
                status_code=404,
                detail="OAuth authorization server is not configured",
            )
        query = request.url.query
        suffix = f"?{query}" if query else ""
        return RedirectResponse(url=f"{auth_base}/authorize{suffix}", status_code=307)

    @router.post("/token", summary="OAuth token endpoint bridge")
    async def oauth_token(request: Request) -> Response:
        return await _proxy_to_oauth_authority(request, settings, "/token")

    @router.post("/revoke", summary="OAuth token revocation bridge")
    async def oauth_revoke(request: Request) -> Response:
        return await _proxy_to_oauth_authority(request, settings, "/revoke")

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
