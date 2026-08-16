"""JWKS key selection must be deterministic during signing-key rotation."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from incidentflow_mcp.auth import oauth


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def _token(private_key: rsa.RSAPrivateKey, *, kid: str | None) -> str:
    header: dict[str, str] = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    payload = {
        "iss": "https://issuer.test.incidentflow.io",
        "aud": "https://mcp.test.incidentflow.io/mcp",
        "exp": int(time.time()) + 300,
        "scope": "mcp:read",
    }
    header_segment = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_segment = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_segment}.{payload_segment}.{_b64url(signature)}"


class _RotatingCache:
    def __init__(self, first: dict[str, Any], second: dict[str, Any]) -> None:
        self._first = first
        self._second = second
        self.force_refreshes: list[bool] = []

    async def get(self, **kwargs: Any) -> dict[str, Any]:
        self.force_refreshes.append(bool(kwargs.get("force_refresh", False)))
        return self._second if kwargs.get("force_refresh") else self._first


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_jwks_once_and_uses_matching_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cache = _RotatingCache(
        {"keys": [_public_jwk(old_key, "old")]},
        {"keys": [_public_jwk(old_key, "old"), _public_jwk(new_key, "new")]},
    )
    monkeypatch.setattr(oauth, "_jwks_cache", cache)

    result = await oauth.validate_oauth_access_token(
        token=_token(new_key, kid="new"),
        jwks_url="https://issuer.test.incidentflow.io/jwks",
        issuer="https://issuer.test.incidentflow.io",
        audience="https://mcp.test.incidentflow.io/mcp",
        required_scope="mcp:read",
        timeout_seconds=1,
    )

    assert result.ok is True
    assert cache.force_refreshes == [False, True]


@pytest.mark.asyncio
async def test_unknown_kid_never_falls_back_to_first_jwks_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwks = {"keys": [_public_jwk(old_key, "old")]}
    cache = _RotatingCache(old_jwks, old_jwks)
    monkeypatch.setattr(oauth, "_jwks_cache", cache)

    result = await oauth.validate_oauth_access_token(
        token=_token(other_key, kid="unknown"),
        jwks_url="https://issuer.test.incidentflow.io/jwks",
        issuer="https://issuer.test.incidentflow.io",
        audience="https://mcp.test.incidentflow.io/mcp",
        required_scope="mcp:read",
        timeout_seconds=1,
    )

    assert result.ok is False
    assert result.detail == "No matching JWKS key"
    assert cache.force_refreshes == [False, True]


@pytest.mark.asyncio
async def test_missing_kid_is_rejected_without_jwks_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cache = _RotatingCache({"keys": [_public_jwk(key, "old")]}, {"keys": []})
    monkeypatch.setattr(oauth, "_jwks_cache", cache)

    result = await oauth.validate_oauth_access_token(
        token=_token(key, kid=None),
        jwks_url="https://issuer.test.incidentflow.io/jwks",
        issuer="https://issuer.test.incidentflow.io",
        audience="https://mcp.test.incidentflow.io/mcp",
        required_scope="mcp:read",
        timeout_seconds=1,
    )

    assert result.ok is False
    assert result.detail == "JWT header has no kid"
    assert cache.force_refreshes == []
