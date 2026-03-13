"""Rate-limit policy models and resolvers.

Identity resolution and policy resolution are intentionally separate.
The default resolver here is OSS-friendly and generic: only
unauthenticated vs authenticated defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from incidentflow_mcp.config import Settings
from incidentflow_mcp.rate_limit.identity import ResolvedIdentity

BucketScope = Literal["ip", "principal", "workspace"]


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    bucket_scope: BucketScope
    transport_limit_per_min: int
    tool_limit_per_min: int
    expensive_tool_limit_per_min: int
    concurrency_limit: int
    timeout_seconds: int


class PolicyResolver(Protocol):
    """Resolve policy and timeouts from identity metadata."""

    def resolve(self, identity: ResolvedIdentity) -> RateLimitPolicy: ...

    def resolve_tool_timeout_seconds(
        self,
        *,
        identity: ResolvedIdentity,
        tool_name: str,
        policy: RateLimitPolicy,
    ) -> int: ...

    def is_expensive_tool(self, tool_name: str) -> bool: ...


class DefaultPolicyResolver:
    """
    Generic policy resolver for the OSS/core server.

    No product-tier semantics are hardcoded here. The default behavior uses
    two neutral policy profiles:
    - default_unauthenticated
    - default_authenticated

    Platform-specific behavior can be introduced later via a replacement
    resolver that implements the PolicyResolver interface.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._expensive_tools = settings.expensive_tools_set()
        self._tool_timeout_overrides = settings.tool_timeout_overrides_map()

        self._unauthenticated_policy = RateLimitPolicy(
            name="default_unauthenticated",
            bucket_scope=_normalize_bucket_scope(settings.rate_limit_unauthenticated_bucket_scope),
            transport_limit_per_min=settings.rate_limit_unauth_per_min,
            tool_limit_per_min=settings.tool_limit_authenticated_per_min,
            expensive_tool_limit_per_min=settings.expensive_tool_limit_per_min,
            concurrency_limit=max(1, settings.tool_concurrency_authenticated),
            timeout_seconds=settings.tool_timeout_seconds,
        )
        self._authenticated_policy = RateLimitPolicy(
            name="default_authenticated",
            bucket_scope=_normalize_bucket_scope(settings.rate_limit_authenticated_bucket_scope),
            transport_limit_per_min=settings.rate_limit_authenticated_per_min,
            tool_limit_per_min=settings.tool_limit_authenticated_per_min,
            expensive_tool_limit_per_min=settings.expensive_tool_limit_per_min,
            concurrency_limit=settings.tool_concurrency_authenticated,
            timeout_seconds=settings.tool_timeout_seconds,
        )

    def resolve(self, identity: ResolvedIdentity) -> RateLimitPolicy:
        if identity.authenticated:
            return self._authenticated_policy
        return self._unauthenticated_policy

    def resolve_tool_timeout_seconds(
        self,
        *,
        identity: ResolvedIdentity,
        tool_name: str,
        policy: RateLimitPolicy,
    ) -> int:
        del identity
        override = self._tool_timeout_overrides.get(tool_name.strip())
        if override is not None:
            return override
        return policy.timeout_seconds

    def is_expensive_tool(self, tool_name: str) -> bool:
        return tool_name.strip() in self._expensive_tools


def _normalize_bucket_scope(raw: str) -> BucketScope:
    value = raw.strip().lower()
    if value in {"ip", "principal", "workspace"}:
        return value  # type: ignore[return-value]
    return "principal"
