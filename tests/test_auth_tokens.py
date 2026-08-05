"""Unit tests for local PAT hashing and verification."""

import hashlib

import pytest

from incidentflow_mcp.auth.tokens import generate_pat, verify_token
from incidentflow_mcp.config import Settings


def _configure_pepper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pepper: str | None,
    version: str = "v1",
) -> None:
    settings = Settings(
        _env_file=None,
        incidentflow_token_pepper=pepper,
        incidentflow_token_pepper_version=version,
    )
    monkeypatch.setattr("incidentflow_mcp.config._settings", settings)


def test_legacy_hash_is_generated_and_verified_without_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pepper(monkeypatch, pepper=None)

    token, _, token_hash = generate_pat()

    assert token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert verify_token(token, token_hash)
    assert not verify_token(f"{token}tampered", token_hash)


def test_legacy_hash_remains_valid_when_pepper_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "if_pat_local_deadbeef.legacy-secret"
    legacy_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    _configure_pepper(monkeypatch, pepper="current-pepper", version="v3")

    assert verify_token(token, legacy_hash)


def test_peppered_hash_carries_version_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pepper(monkeypatch, pepper="current-pepper", version="v3")

    token, _, token_hash = generate_pat()
    version, digest = token_hash.split(":", 1)

    assert version == "v3"
    assert len(digest) == 64
    assert token_hash != hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert verify_token(token, token_hash)
    assert not verify_token(f"{token}tampered", token_hash)


@pytest.mark.parametrize(
    ("pepper", "version"),
    [
        ("rotated-pepper", "v3"),
        ("current-pepper", "v4"),
        (None, "v3"),
    ],
)
def test_peppered_hash_fails_closed_when_configuration_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
    pepper: str | None,
    version: str,
) -> None:
    _configure_pepper(monkeypatch, pepper="current-pepper", version="v3")
    token, _, token_hash = generate_pat()

    _configure_pepper(monkeypatch, pepper=pepper, version=version)

    assert not verify_token(token, token_hash)


@pytest.mark.parametrize("expected_hash", ["v3:", ":digest", "v3:not-a-valid-digest"])
def test_malformed_peppered_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    expected_hash: str,
) -> None:
    _configure_pepper(monkeypatch, pepper="current-pepper", version="v3")

    assert not verify_token("if_pat_local_deadbeef.secret", expected_hash)
