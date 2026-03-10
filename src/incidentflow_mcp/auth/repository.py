"""
Token persistence layer.

Defines an abstract TokenRepository interface so the storage backend can be
swapped without touching any other module.

Current implementation: JsonTokenRepository
    Default path: ~/.incidentflow/tokens.json
    Override via:  INCIDENTFLOW_TOKEN_DB=/path/to/tokens.json

Future: implement PostgresTokenRepository satisfying the same interface to
migrate from local dev to hosted deployment with zero changes to auth logic.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Datetime serialisation (always UTC ISO-8601)
# ---------------------------------------------------------------------------

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


# ---------------------------------------------------------------------------
# Token record
# ---------------------------------------------------------------------------


@dataclass
class TokenRecord:
    """
    Persisted token metadata.  Never contains the plaintext token.

    Supported scopes (extend as needed):
        mcp:read        — read MCP resources
        mcp:tools:run   — execute MCP tools
        admin           — administrative operations

    Future OAuth scope mapping:
        When integrating an OAuth authorization server, map the scopes
        returned by introspection to these local scope strings here,
        or add an OAuth-native scope string and check for it in the
        middleware.
    """

    token_id: str
    token_hash: str
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class TokenRepository(ABC):
    """
    Abstract token store.

    Implement this interface to add a new storage backend.

    Future: PostgresTokenRepository, RedisTokenRepository, etc.
    """

    @abstractmethod
    def save(self, record: TokenRecord) -> None: ...

    @abstractmethod
    def find_by_id(self, token_id: str) -> Optional[TokenRecord]: ...

    @abstractmethod
    def list_all(self) -> list[TokenRecord]: ...

    @abstractmethod
    def update_last_used(self, token_id: str, at: datetime) -> None: ...

    @abstractmethod
    def revoke(self, token_id: str, at: datetime) -> None:
        """Raises KeyError if the token_id does not exist."""
        ...


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_ISO_FMT) if dt is not None else None


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.strptime(s, _ISO_FMT).replace(tzinfo=timezone.utc)


def _to_dict(r: TokenRecord) -> dict:
    return {
        "token_id": r.token_id,
        "token_hash": r.token_hash,
        "name": r.name,
        "scopes": r.scopes,
        "created_at": _dt_to_str(r.created_at),
        "last_used_at": _dt_to_str(r.last_used_at),
        "expires_at": _dt_to_str(r.expires_at),
        "revoked_at": _dt_to_str(r.revoked_at),
    }


def _from_dict(d: dict) -> TokenRecord:
    return TokenRecord(
        token_id=d["token_id"],
        token_hash=d["token_hash"],
        name=d["name"],
        scopes=d.get("scopes", []),
        created_at=_str_to_dt(d["created_at"]),  # type: ignore[arg-type]
        last_used_at=_str_to_dt(d.get("last_used_at")),
        expires_at=_str_to_dt(d.get("expires_at")),
        revoked_at=_str_to_dt(d.get("revoked_at")),
    )


# ---------------------------------------------------------------------------
# JSON-file backend
# ---------------------------------------------------------------------------


class JsonTokenRepository(TokenRepository):
    """
    JSON-file-backed token store for local development.

    The file is read/written on every operation — simple, no in-memory state,
    works correctly across multiple CLI invocations.

    Default path: ~/.incidentflow/tokens.json
    Override via: INCIDENTFLOW_TOKEN_DB env var or constructor argument.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            env = os.environ.get("INCIDENTFLOW_TOKEN_DB")
            path = Path(env) if env else Path.home() / ".incidentflow" / "tokens.json"
        self._path = path

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _persist(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    def save(self, record: TokenRecord) -> None:
        data = self._load()
        data[record.token_id] = _to_dict(record)
        self._persist(data)

    def find_by_id(self, token_id: str) -> Optional[TokenRecord]:
        entry = self._load().get(token_id)
        return _from_dict(entry) if entry is not None else None

    def list_all(self) -> list[TokenRecord]:
        return [_from_dict(v) for v in self._load().values()]

    def update_last_used(self, token_id: str, at: datetime) -> None:
        data = self._load()
        if token_id in data:
            data[token_id]["last_used_at"] = _dt_to_str(at)
            self._persist(data)

    def revoke(self, token_id: str, at: datetime) -> None:
        data = self._load()
        if token_id not in data:
            raise KeyError(f"Token {token_id!r} not found")
        data[token_id]["revoked_at"] = _dt_to_str(at)
        self._persist(data)


# ---------------------------------------------------------------------------
# In-memory backend (tests / ephemeral use)
# ---------------------------------------------------------------------------


class InMemoryTokenRepository(TokenRepository):
    """
    In-memory token store for testing and ephemeral use.

    Not persistent across process restarts.  Not thread-safe (good enough
    for single-threaded test suites).
    """

    def __init__(self) -> None:
        self._store: dict[str, TokenRecord] = {}

    def save(self, record: TokenRecord) -> None:
        self._store[record.token_id] = record

    def find_by_id(self, token_id: str) -> Optional[TokenRecord]:
        return self._store.get(token_id)

    def list_all(self) -> list[TokenRecord]:
        return list(self._store.values())

    def update_last_used(self, token_id: str, at: datetime) -> None:
        if token_id in self._store:
            self._store[token_id].last_used_at = at

    def revoke(self, token_id: str, at: datetime) -> None:
        if token_id not in self._store:
            raise KeyError(f"Token {token_id!r} not found")
        self._store[token_id].revoked_at = at


# ---------------------------------------------------------------------------
# Repository factory (singleton, monkeypatchable in tests)
# ---------------------------------------------------------------------------

_repo: Optional[TokenRepository] = None


def get_token_repository() -> TokenRepository:
    """
    Return the cached token repository singleton.

    Override in tests by monkeypatching this module's ``_repo`` variable:

        monkeypatch.setattr("incidentflow_mcp.auth.repository._repo",
                            InMemoryTokenRepository())
    """
    global _repo
    if _repo is None:
        _repo = JsonTokenRepository()
    return _repo
