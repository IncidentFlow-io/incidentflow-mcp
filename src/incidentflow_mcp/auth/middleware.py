"""Bearer authentication middleware with dual OAuth+PAT mode."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from datetime import datetime, timezone

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.auth.oauth import validate_oauth_access_token
from incidentflow_mcp.auth.repository import get_token_repository
from incidentflow_mcp.auth.tokens import parse_token_id, verify_token
from incidentflow_mcp.config import get_settings

logger = logging.getLogger(__name__)

_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/readyz",
    "/install.sh",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/jwks.json",
    "/register",
    "/oauth/register",
})

_MCP_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "POST", "OPTIONS"})

_SCOPE_POLICY: list[tuple[str, str]] = [
    ("/admin", "admin"),
    ("/mcp/tools", "mcp:tools:run"),
    ("/mcp/resources", "mcp:read"),
    ("/mcp", "mcp:read"),
]


def _required_scope_for_request(request: Request) -> str | None:
    path = request.url.path
    for prefix, scope in _SCOPE_POLICY:
        if path.startswith(prefix):
            return scope
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        clear_current_auth_context()
        try:
            if request.url.path == "/mcp" and request.method.upper() not in _MCP_ALLOWED_METHODS:
                return await call_next(request)

            if request.url.path in _PUBLIC_PATHS:
                return await call_next(request)

            error = await _verify_bearer(request)
            if error is not None:
                return error

            return await call_next(request)
        finally:
            clear_current_auth_context()


async def _verify_bearer(request: Request) -> JSONResponse | None:
    _set_auth_context(request, authenticated=False)

    if "token" in request.query_params or "access_token" in request.query_params:
        logger.warning("auth: rejected token sent via query parameter from %s", _client_ip(request))
        return _unauthorized("Tokens must be sent in the Authorization header, not as query parameters.")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        if request.url.path == "/metrics" and _is_metrics_request_allowed_without_auth(request):
            return None
        if not _any_auth_configured():
            logger.warning("auth: no auth provider configured — MCP endpoint is UNPROTECTED")
            return None
        logger.warning("auth: missing Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    if not auth_header.lower().startswith("bearer "):
        logger.warning("auth: malformed Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    token = auth_header[len("bearer ") :].strip()
    if not token:
        return _unauthorized("Empty Bearer token.")

    required_scope = _required_scope_for_request(request)

    oauth_check = await _attempt_oauth_validation(request=request, token=token, required_scope=required_scope)
    if oauth_check is not None:
        if oauth_check.status_code == 200:
            return None
        return oauth_check

    pat_check = await introspect_managed_pat(request=request, token=token, required_scope=required_scope)
    if pat_check is not None:
        if pat_check.status_code == 200:
            return None
        if pat_check.status_code != 404:
            return pat_check

    local_check = validate_local_pat(request=request, token=token, required_scope=required_scope)
    if local_check is not None:
        if local_check.status_code == 200:
            return None
        if local_check.status_code != 404:
            return local_check

    static_check = validate_static_pat(request=request, token=token)
    if static_check is not None:
        if static_check.status_code == 200:
            return None
        return static_check

    if not _any_auth_configured():
        logger.warning("auth: no auth provider configured — MCP endpoint is UNPROTECTED")
        return None

    return _unauthorized("Invalid token.", required_scope=required_scope)


async def _attempt_oauth_validation(
    *,
    request: Request,
    token: str,
    required_scope: str | None,
) -> JSONResponse | None:
    settings = get_settings()
    if not settings.oauth_validation_enabled():
        return None

    result = await validate_oauth_access_token(
        token=token,
        jwks_url=str(settings.oauth_jwks_url),
        issuer=str(settings.oauth_expected_issuer),
        audience=settings.mcp_canonical_resource,
        required_scope=required_scope,
        timeout_seconds=settings.platform_api_timeout_seconds,
    )

    if result.ok:
        claims = result.claims or {}
        _set_auth_context(
            request,
            authenticated=True,
            client_id=str(claims.get("client_id") or "oauth_client"),
            workspace_id=(str(claims.get("workspace_id")) if claims.get("workspace_id") else None),
            user_id=(str(claims.get("user_id")) if claims.get("user_id") else None),
            plan=None,
        )
        return JSONResponse(status_code=200, content={})

    if result.code == "insufficient_scope":
        return _unauthorized("Insufficient token scope", required_scope=required_scope)

    # This looks like OAuth but failed validation; do not downcast to PAT.
    if result.code == "oauth_invalid":
        return _unauthorized(result.detail, required_scope=required_scope)

    # not_oauth -> continue with PAT fallback.
    return None


async def introspect_managed_pat(
    *,
    request: Request,
    token: str,
    required_scope: str | None,
) -> JSONResponse | None:
    settings = get_settings()
    if not settings.managed_token_introspection_enabled():
        return JSONResponse(status_code=404, content={})

    url = f"{settings.platform_api_base_url.rstrip('/')}{settings.platform_api_introspect_path}"
    payload = {"required_scope": required_scope}

    try:
        async with httpx.AsyncClient(timeout=settings.platform_api_timeout_seconds) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("auth: platform-api introspection failed (%s): %s", url, str(exc))
        return _service_unavailable("Token verification service unavailable")

    if response.status_code == 200:
        data = response.json()
        _set_auth_context(
            request,
            authenticated=True,
            client_id=data.get("credential_id"),
            workspace_id=data.get("workspace_id"),
            user_id=data.get("user_id"),
            plan=None,
        )
        return JSONResponse(status_code=200, content={})

    if response.status_code == 403:
        return _unauthorized("Insufficient token scope", required_scope=required_scope)

    if response.status_code == 401:
        return JSONResponse(status_code=404, content={})

    logger.warning("auth: unexpected introspection status=%s body=%s", response.status_code, response.text)
    return _service_unavailable("Token verification service error")


def validate_local_pat(
    *,
    request: Request,
    token: str,
    required_scope: str | None,
) -> JSONResponse | None:
    token_id = parse_token_id(token)
    if token_id is None:
        return JSONResponse(status_code=404, content={})

    repo = get_token_repository()
    record = repo.find_by_id(token_id)
    if record is None:
        logger.warning("auth: unknown token_id %r from %s", token_id, _client_ip(request))
        return _unauthorized("Invalid token.", required_scope=required_scope)

    if record.revoked_at is not None:
        logger.warning("auth: revoked token %r used from %s", token_id, _client_ip(request))
        return _unauthorized("Token has been revoked.", required_scope=required_scope)

    now = datetime.now(timezone.utc)
    if record.expires_at is not None and record.expires_at < now:
        logger.warning("auth: expired token %r used from %s", token_id, _client_ip(request))
        return _unauthorized("Token has expired.", required_scope=required_scope)

    if not verify_token(token, record.token_hash):
        logger.warning("auth: invalid token secret for %r from %s", token_id, _client_ip(request))
        return _unauthorized("Invalid token.", required_scope=required_scope)

    if required_scope is not None and required_scope not in record.scopes and get_settings().scopes_enforced():
        logger.warning("auth_scope_denied token_id=%s required_scope=%s", token_id, required_scope)
        return _unauthorized("Insufficient token scope", required_scope=required_scope)

    repo.update_last_used(token_id, now)
    _set_auth_context(
        request,
        authenticated=True,
        client_id=token_id,
        workspace_id=request.headers.get("x-workspace-id"),
        user_id=request.headers.get("x-user-id"),
        plan=(request.headers.get("x-plan") or request.headers.get("x-plan-tier") or request.headers.get("x-tier")),
    )
    return JSONResponse(status_code=200, content={})


def validate_static_pat(*, request: Request, token: str) -> JSONResponse | None:
    settings = get_settings()
    expected_pat = settings.incidentflow_pat
    if expected_pat is None:
        return None

    expected = expected_pat.get_secret_value()
    if not hmac.compare_digest(token.encode(), expected.encode()):
        logger.warning("auth: invalid static PAT token from %s", _client_ip(request))
        return _unauthorized("Invalid token.")

    _set_auth_context(request, authenticated=True, client_id="legacy_pat")
    return JSONResponse(status_code=200, content={})


def _any_auth_configured() -> bool:
    settings = get_settings()
    if settings.oauth_validation_enabled():
        return True
    if settings.managed_token_introspection_enabled():
        return True
    if settings.incidentflow_pat is not None:
        return True
    return bool(get_token_repository().list_all())


def _www_authenticate_value(required_scope: str | None = None) -> str:
    settings = get_settings()
    base = f'Bearer resource_metadata="{settings.mcp_resource_metadata_url}"'
    if required_scope:
        return f'{base}, scope="{required_scope}"'
    return base


def _unauthorized(detail: str, *, required_scope: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": _www_authenticate_value(required_scope)},
    )


def _service_unavailable(detail: str) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": detail})


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _is_metrics_request_allowed_without_auth(request: Request) -> bool:
    client_ip = _client_ip(request)
    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    settings = get_settings()
    for cidr in settings.metrics_trusted_cidrs_list():
        try:
            if ip_obj in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning("auth: invalid CIDR in METRICS_TRUSTED_CIDRS: %s", cidr)
            continue
    return False


def _set_auth_context(
    request: Request,
    *,
    authenticated: bool,
    client_id: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
    plan: str | None = None,
) -> None:
    context = {
        "authenticated": authenticated,
        "client_id": client_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "plan": plan,
    }
    request.state.auth_context = context
    set_current_auth_context(context)
