"""Identity resolution for rate limiting.

This module is intentionally policy-agnostic:
- resolves identity and raw plan metadata
- does not map plans/tiers to product semantics
- does not decide bucket scope or limits
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class ResolvedIdentity:
    authenticated: bool
    ip_address: str
    workspace_id: str | None
    user_id: str | None
    client_id: str | None
    plan: str | None

    @property
    def principal_key(self) -> str:
        """
        Stable principal key for policy and bucket-key layers.

        Resolution order:
        1) workspace + user
        2) client id
        3) IP address
        """
        if self.workspace_id and self.user_id:
            return f"workspace:{self.workspace_id}:user:{self.user_id}"
        if self.client_id:
            return f"client:{self.client_id}"
        return f"ip:{self.ip_address}"


class IdentityResolver:
    """Resolve identity from auth context, headers, and client network metadata."""

    def resolve(self, request: Request) -> ResolvedIdentity:
        auth_ctx = self._auth_context(request)

        return ResolvedIdentity(
            authenticated=bool(auth_ctx.get("authenticated", False)),
            ip_address=_client_ip(request),
            workspace_id=_normalize_value(auth_ctx.get("workspace_id") or request.headers.get("x-workspace-id")),
            user_id=_normalize_value(auth_ctx.get("user_id") or request.headers.get("x-user-id")),
            client_id=_normalize_value(auth_ctx.get("client_id") or request.headers.get("x-client-id")),
            plan=self._resolve_plan(auth_ctx, request),
        )

    @staticmethod
    def _auth_context(request: Request) -> dict[str, object]:
        raw = getattr(request.state, "auth_context", None)
        if isinstance(raw, dict):
            return raw
        return {}

    @staticmethod
    def _resolve_plan(auth_ctx: dict[str, object], request: Request) -> str | None:
        plan_sources: list[object] = [
            auth_ctx.get("plan"),
            auth_ctx.get("tier"),
            request.headers.get("x-plan"),
            request.headers.get("x-plan-tier"),
            request.headers.get("x-tier"),
        ]
        for value in plan_sources:
            normalized = _normalize_value(value)
            if normalized is not None:
                return normalized
        return None


def _normalize_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
