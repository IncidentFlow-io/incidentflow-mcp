"""
Bearer PAT authentication for local HTTP dev mode.

Strategy
--------
Two verification paths are tried in order:

1. **Structured repo token** (``if_pat_local_<id>.<secret>``):
   Parse the token_id, look up the record in the TokenRepository, verify
   the SHA-256 hash with constant-time comparison, enforce revocation and
   expiry, check required scopes, and update last_used_at on success.

2. **Legacy env PAT** (any other token string):
   Fall back to a direct constant-time comparison with INCIDENTFLOW_PAT
   from settings.  Useful for quick local scripting without managing a
   token DB entry.  Legacy tokens are not scope-checked.

3. **UNPROTECTED mode**:
   If neither a matching repo token nor INCIDENTFLOW_PAT is configured, the
   server allows all requests and logs a warning on each.

Tokens via query parameters are explicitly rejected in all cases.

Scope enforcement
-----------------
Each endpoint maps to a required scope via ``_required_scope_for_request()``.
When ``settings.scopes_enforced()`` is True (default in production), a token
missing the required scope gets 403.  In dev mode (enforcement disabled) the
scope mismatch is only logged as a warning.

Future OAuth resource-server integration point
-----------------------------------------------
Replace ``_verify_repo_token()`` with a call to your authorization server's
token introspection endpoint (RFC 7662).  The request-extraction logic and
error-response helpers stay the same.
"""

import hmac
import logging
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from incidentflow_mcp.auth.repository import get_token_repository
from incidentflow_mcp.auth.tokens import parse_token_id, verify_token
from incidentflow_mcp.config import get_settings

logger = logging.getLogger(__name__)

# Paths that do NOT require authentication
_PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/docs", "/openapi.json", "/redoc"})

# ---------------------------------------------------------------------------
# Scope policy — maps request path prefixes to the required token scope.
# The first match wins; None means no scope is required beyond authentication.
# ---------------------------------------------------------------------------
_SCOPE_POLICY: list[tuple[str, str]] = [
    ("/admin", "admin"),
    ("/mcp/tools", "mcp:tools:run"),
    ("/mcp/resources", "mcp:read"),
    ("/mcp", "mcp:read"),        # catch-all for the MCP endpoint
]


def _required_scope_for_request(request: Request) -> str | None:
    """Return the scope required to access this endpoint, or None."""
    path = request.url.path
    for prefix, scope in _SCOPE_POLICY:
        if path.startswith(prefix):
            return scope
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces Bearer PAT authentication on all
    non-public paths.

    Mount order: this middleware should be added to the FastAPI app AFTER
    any trusted infrastructure middleware (e.g. ProxyHeaders) but BEFORE
    any business-logic middleware.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        error = _verify_bearer(request)
        if error is not None:
            return error

        return await call_next(request)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verify_bearer(request: Request) -> JSONResponse | None:
    """
    Validate the Bearer token on the incoming request.

    Returns None on success, or a JSONResponse with 401 on failure.
    """
    # Guard: tokens via query parameters are explicitly forbidden
    if "token" in request.query_params or "access_token" in request.query_params:
        logger.warning("auth: rejected token sent via query parameter from %s", _client_ip(request))
        return _unauthorized("Tokens must be sent in the Authorization header, not as query parameters.")

    auth_header = request.headers.get("Authorization", "")

    # No auth header — allow only if nothing is configured (unprotected mode)
    if not auth_header:
        if not _any_auth_configured():
            logger.warning("auth: INCIDENTFLOW_PAT is not set — server running in UNPROTECTED mode")
            return None
        logger.warning("auth: missing Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    if not auth_header.lower().startswith("bearer "):
        logger.warning("auth: malformed Authorization header from %s", _client_ip(request))
        return _unauthorized("Missing or malformed Authorization: Bearer <token>.")

    provided = auth_header[len("bearer "):].strip()
    if not provided:
        return _unauthorized("Empty Bearer token.")

    # --- Path 1: structured repo token (if_pat_local_<id>.<secret>) ---
    token_id = parse_token_id(provided)
    if token_id is not None:
        return _verify_repo_token(provided, token_id, request)

    # --- Path 2: legacy plain token → compare with INCIDENTFLOW_PAT ---
    settings = get_settings()
    expected_pat = settings.incidentflow_pat

    if expected_pat is None:
        # Plain (non-structured) token, but no INCIDENTFLOW_PAT set → unprotected
        logger.warning("auth: INCIDENTFLOW_PAT is not set — server running in UNPROTECTED mode")
        return None

    expected = expected_pat.get_secret_value()
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("auth: invalid token from %s", _client_ip(request))
        return _unauthorized("Invalid token.")

    return None


def _verify_repo_token(token: str, token_id: str, request: Request) -> JSONResponse | None:
    """
    Look up the token record, enforce revocation/expiry, verify the hash,
    check scopes, and update last_used_at on success.

    Future OAuth integration point: replace body with an introspection call.
    """
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

    # --- Scope check ---
    required = _required_scope_for_request(request)
    if required is not None:
        if required in record.scopes:
            logger.info("auth_scope_granted token_id=%s scope=%s", token_id, required)
        elif get_settings().scopes_enforced():
            logger.warning("auth_scope_denied token_id=%s required_scope=%s", token_id, required)
            return _forbidden(required)
        else:
            logger.warning(
                "auth_scope_bypass token_id=%s required_scope=%s (enforcement disabled — dev mode)",
                token_id,
                required,
            )

    repo.update_last_used(token_id, now)
    return None


def _any_auth_configured() -> bool:
    """
    Return True if the server has at least one authentication mechanism enabled.

    Checked on every request that arrives without an Authorization header, so
    the implementation intentionally stays cheap for the common (protected) case.
    """
    if get_settings().incidentflow_pat is not None:
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


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"

