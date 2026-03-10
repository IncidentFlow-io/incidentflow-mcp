"""
Token generation and verification for local HTTP dev mode.

Token format:  if_pat_local_<token_id>.<secret>
Example:       if_pat_local_a1b2c3d4.Nx8K3mQp7Wz2Rk9Ls1Vf0Yt6AbCdEfGh

The token_id is a short public identifier used for fast DB lookup.
The secret is the high-entropy part that proves possession.
Only the token_hash (SHA-256) of the full token string is ever persisted.

Future OAuth resource-server integration point
----------------------------------------------
Replace verify_token() / _hash_token() with calls to your authorization
server's token introspection endpoint (RFC 7662). The public interface
(generate_pat, verify_token, parse_token_id) stays identical so no
callers need to change.
"""

import hashlib
import hmac
import secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFIX = "if_pat_local_"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_pat() -> tuple[str, str, str]:
    """
    Generate a new Personal Access Token.

    Returns:
        (plaintext_token, token_id, token_hash)

    The plaintext_token MUST be shown to the user exactly once and NEVER
    stored.  Only token_id and token_hash should be persisted.
    """
    token_id = secrets.token_hex(4)        # 8 hex chars — short, safe for lookup
    secret = secrets.token_urlsafe(32)     # 43 url-safe base64 chars
    token = f"{PREFIX}{token_id}.{secret}"
    token_hash = _hash_token(token)
    return token, token_id, token_hash


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    """
    Hash a token for at-rest storage using SHA-256.

    Extension point — to add a pepper:
        pepper = os.environ.get("INCIDENTFLOW_TOKEN_PEPPER", "")
        value = (pepper + token).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    Future: swap for Argon2id / bcrypt for multi-user hosted deployments.
    """
    # TODO: add INCIDENTFLOW_TOKEN_PEPPER support when moving to hosted mode
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_token(token: str, expected_hash: str) -> bool:
    """
    Verify a full PAT string against its stored hash.

    Uses hmac.compare_digest for constant-time comparison to prevent
    timing-based side-channel attacks.

    Future OAuth integration point:
        Replace this function body with an introspection call when
        switching from local PATs to OAuth access tokens.
    """
    actual_hash = _hash_token(token)
    return hmac.compare_digest(actual_hash, expected_hash)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_token_id(token: str) -> str | None:
    """
    Extract the public token_id from a full PAT string.

    Returns None if the token does not match the expected format.
    The token_id is used to look up the stored record before verification.
    """
    if not token.startswith(PREFIX):
        return None
    rest = token[len(PREFIX):]
    parts = rest.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0]
