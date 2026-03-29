"""OAuth access token validation for MCP resource server."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@dataclass(slots=True)
class OAuthValidationResult:
    ok: bool
    code: str
    detail: str
    claims: dict[str, Any] | None = None


def _b64url_decode(value: str) -> bytes:
    padding_len = (-len(value)) % 4
    value += "=" * padding_len
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _decode_json_segment(segment: str) -> dict[str, Any]:
    return json.loads(_b64url_decode(segment).decode("utf-8"))


class JwksCache:
    def __init__(self) -> None:
        self._jwks: dict[str, Any] | None = None
        self._expires_at: float = 0.0

    async def get(self, *, jwks_url: str, timeout_seconds: float, ttl_seconds: int = 300) -> dict[str, Any]:
        now = time.time()
        if self._jwks is not None and now < self._expires_at:
            return self._jwks

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            payload = response.json()

        self._jwks = payload
        self._expires_at = now + ttl_seconds
        return payload


_jwks_cache = JwksCache()


async def validate_oauth_access_token(
    *,
    token: str,
    jwks_url: str,
    issuer: str,
    audience: str,
    required_scope: str | None,
    timeout_seconds: float,
) -> OAuthValidationResult:
    segments = token.split(".")
    if len(segments) != 3:
        return OAuthValidationResult(ok=False, code="not_oauth", detail="Token is not a JWT")

    header_segment, payload_segment, signature_segment = segments
    try:
        header = _decode_json_segment(header_segment)
        claims = _decode_json_segment(payload_segment)
        signature = _b64url_decode(signature_segment)
    except Exception:
        return OAuthValidationResult(ok=False, code="not_oauth", detail="Malformed JWT token")

    alg = str(header.get("alg", ""))
    if alg != "RS256":
        return OAuthValidationResult(ok=False, code="not_oauth", detail="Unsupported JWT alg")

    jwks = await _jwks_cache.get(jwks_url=jwks_url, timeout_seconds=timeout_seconds)
    keys = jwks.get("keys", []) if isinstance(jwks, dict) else []
    kid = str(header.get("kid", ""))
    key = None
    if kid:
        for candidate in keys:
            if isinstance(candidate, dict) and str(candidate.get("kid", "")) == kid:
                key = candidate
                break
    if key is None and keys:
        maybe_first = keys[0]
        if isinstance(maybe_first, dict):
            key = maybe_first

    if key is None:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="No matching JWKS key")

    try:
        n_raw = _b64url_decode(str(key["n"]))
        e_raw = _b64url_decode(str(key["e"]))
        public_numbers = rsa.RSAPublicNumbers(
            e=int.from_bytes(e_raw, "big"),
            n=int.from_bytes(n_raw, "big"),
        )
        public_key = public_numbers.public_key()
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="Invalid token signature")

    now = int(time.time())
    token_iss = str(claims.get("iss", ""))
    token_aud = claims.get("aud")
    exp = claims.get("exp")
    nbf = claims.get("nbf")

    if token_iss != issuer:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="Invalid token issuer")

    aud_ok = False
    if isinstance(token_aud, str):
        aud_ok = token_aud == audience
    elif isinstance(token_aud, list):
        aud_ok = audience in [str(item) for item in token_aud]
    if not aud_ok:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="Invalid token audience/resource")

    if not isinstance(exp, int) or exp <= now:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="Token expired")

    if isinstance(nbf, int) and nbf > now:
        return OAuthValidationResult(ok=False, code="oauth_invalid", detail="Token is not active yet")

    if required_scope is not None:
        raw_scope = claims.get("scope", "")
        scopes: list[str]
        if isinstance(raw_scope, str):
            scopes = [item for item in raw_scope.split() if item]
        elif isinstance(raw_scope, list):
            scopes = [str(item) for item in raw_scope]
        else:
            scopes = []
        if required_scope not in scopes:
            return OAuthValidationResult(
                ok=False,
                code="insufficient_scope",
                detail="Insufficient token scope",
                claims=claims,
            )

    return OAuthValidationResult(ok=True, code="ok", detail="ok", claims=claims)
