"""Bucket key resolution for rate limiting.

Bucket selection is a policy decision, not an identity concern.
"""

from __future__ import annotations

from incidentflow_mcp.rate_limit.identity import ResolvedIdentity
from incidentflow_mcp.rate_limit.policy import RateLimitPolicy


class BucketKeyResolver:
    """Compute transport/tool/concurrency keys from identity + policy."""

    def transport_key(self, identity: ResolvedIdentity, policy: RateLimitPolicy) -> str:
        return self._base_key(identity, policy)

    def tool_key(self, identity: ResolvedIdentity, policy: RateLimitPolicy) -> str:
        return self._base_key(identity, policy)

    def concurrency_key(self, identity: ResolvedIdentity, policy: RateLimitPolicy) -> str:
        return self._base_key(identity, policy)

    @staticmethod
    def _base_key(identity: ResolvedIdentity, policy: RateLimitPolicy) -> str:
        scope = policy.bucket_scope

        if scope == "workspace" and identity.workspace_id:
            return f"workspace:{identity.workspace_id}"
        if scope == "principal":
            return identity.principal_key
        return f"ip:{identity.ip_address}"
