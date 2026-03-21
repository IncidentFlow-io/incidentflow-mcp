"""
Bearer authentication middleware.

Verification strategy (in order):
1. Managed-token introspection against platform-api (if configured)
2. Structured local repo token (if_pat_local_<id>.<secret>)
3. Legacy INCIDENTFLOW_PAT constant-time comparison
4. Unprotected mode only when no auth source is configured
"""

import hmac
import logging
from datetime import datetime, timezone

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.auth.repository import get_token_repository
from incidentflow_mcp.auth.tokens import parse_token_id, verify_token
from incidentflow_mcp.config import get_settings

logger = logging.getLogger(__name__)

# Paths that do NOT require authentication
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/readyz",
    "/install.sh",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/authorize",
    "/token",
    "/register",
    "/oauth/register",
})

_MCP_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "POST", "OPTIONS"})

# ---------------------------------------------------------------------------
# Scope policy — maps request path prefixes to required scopes.
# ---------------------------------------------------------------------------
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
                # Let router return 404/405 for unsupported methods.
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
        if not _any_auth_configured():
            logger.warning("auth: no auth provider configured — MCP endpoint is UNPROTECTED")
            return None
        logger.warning("auth: missing Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    if not auth_header.lower().startswith("bearer "):
        logger.warning("auth: malformed Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    provided = auth_header[len("bearer "):].strip()
    if not provided:
        return _unauthorized("Empty Bearer token.")

    required_scope = _required_scope_for_request(request)
    settings = get_settings()

    # Path 1: managed token introspection via platform-api.
    if settings.managed_token_introspection_enabled():
        return await _verify_platform_api_token(
            request=request,
            token=provided,
            required_scope=required_scope,
        )

    # Path 2: structured local repo token.
    token_id = parse_token_id(provided)
    if token_id is not None:
        return _verify_repo_token(provided, token_id, request, required_scope=required_scope)

    # Path 3: legacy env PAT.
    expected_pat = settings.incidentflow_pat
    if expected_pat is None:
        if not _any_auth_configured():
            logger.warning("auth: no auth provider configured — MCP endpoint is UNPROTECTED")
            return None
        logger.warning("auth: invalid token from %s", _client_ip(request))
        return _unauthorized("Invalid token.")

    expected = expected_pat.get_secret_value()
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("auth: invalid token from %s", _client_ip(request))
        return _unauthorized("Invalid token.")

    _set_auth_context(request, authenticated=True, client_id="legacy_pat")
    return None


async def _verify_platform_api_token(
    *,
    request: Request,
    token: str,
    required_scope: str | None,
) -> JSONResponse | None:
    settings = get_settings()
    if not settings.platform_api_base_url:
        return _unauthorized("Managed token introspection is not configured.")

    url = f"{settings.platform_api_base_url.rstrip('/')}{settings.platform_api_introspect_path}"
    payload = {"required_scope": required_scope}

    try:
        async with httpx.AsyncClient(timeout=settings.platform_api_timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
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
        return None

    if response.status_code == 403:
        message = "Insufficient token scope"
        try:
            body = response.json()
            message = body.get("message") or message
        except ValueError:
            pass
        return _forbidden_detail(message)

    if response.status_code == 401:
        return _unauthorized("Invalid token.")

    logger.warning("auth: unexpected introspection status=%s body=%s", response.status_code, response.text)
    return _service_unavailable("Token verification service error")


def _verify_repo_token(
    token: str,
    token_id: str,
    request: Request,
    *,
    required_scope: str | None,
) -> JSONResponse | None:
    repo = get_token_repository()
    record = repo.find_by_id(token_id)

    if record is None:
        logger.warning("auth: unknown token_id %r from %s", token_id, _client_ip(request))
        return _unauthorized("Invalid token.")

    if record.revoked_at is not None:
        logger.warning("auth: revoked token %r used from %s", token_id, _client_ip(request))
        return _unauthorized("Token has been revoked.")

    now = datetime.now(timezone.utc)
    if record.expires_at is not None and record.expires_at < now:
        logger.warning("auth: expired token %r used from %s", token_id, _client_ip(request))
        return _unauthorized("Token has expired.")

    if not verify_token(token, record.token_hash):
        logger.warning("auth: invalid token secret for %r from %s", token_id, _client_ip(request))
        return _unauthorized("Invalid token.")

    if required_scope is not None:
        if required_scope in record.scopes:
            logger.info("auth_scope_granted token_id=%s scope=%s", token_id, required_scope)
        elif get_settings().scopes_enforced():
            logger.warning("auth_scope_denied token_id=%s required_scope=%s", token_id, required_scope)
            return _forbidden(required_scope)
        else:
            logger.warning(
                "auth_scope_bypass token_id=%s required_scope=%s (enforcement disabled — dev mode)",
                token_id,
                required_scope,
            )

    repo.update_last_used(token_id, now)
    _set_auth_context(
        request,
        authenticated=True,
        client_id=token_id,
        workspace_id=request.headers.get("x-workspace-id"),
        user_id=request.headers.get("x-user-id"),
        plan=(
            request.headers.get("x-plan")
            or request.headers.get("x-plan-tier")
            or request.headers.get("x-tier")
        ),
    )
    return None


def _any_auth_configured() -> bool:
    settings = get_settings()
    if settings.managed_token_introspection_enabled():
        return True
    if settings.incidentflow_pat is not None:
        return True
    return bool(get_token_repository().list_all())


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(required_scope: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"error": "insufficient_scope", "required_scope": required_scope},
    )


def _forbidden_detail(detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": detail})


def _service_unavailable(detail: str) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": detail})


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


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
