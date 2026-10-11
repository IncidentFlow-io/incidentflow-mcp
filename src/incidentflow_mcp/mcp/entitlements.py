"""MCP-side policy application; Platform remains the authority for values."""

from incidentflow_mcp.mcp.errors import tool_error
from incidentflow_mcp.platform_api.entitlements_client import (
    EntitlementsUnavailable,
    PlatformEntitlementsClient,
)
from incidentflow_mcp.tools.contracts import ErrorCode


async def require_workspace_capability(
    client: PlatformEntitlementsClient,
    *, workspace_id: str,
    write: bool = False,
    limit_key: str | None = None,
    feature_key: str | None = None,
) -> dict | None:
    try:
        snapshot = await client.get(workspace_id, allow_cached=not write)
    except EntitlementsUnavailable:
        return tool_error(
            ErrorCode.ENTITLEMENTS_UNAVAILABLE,
            "Workspace entitlements are temporarily unavailable",
            retryable=True,
        )
    if not snapshot.writable:
        return tool_error(
            ErrorCode.BILLING_REMEDIATION_REQUIRED,
            "Billing needs attention before this workspace action can run",
            retryable=False,
            details={"upgrade_url": snapshot.upgrade_url},
        )
    if feature_key and not snapshot.features.get(feature_key, False):
        return tool_error(
            ErrorCode.BILLING_REMEDIATION_REQUIRED,
            "This capability is not included in the current plan",
            retryable=False,
            details={"upgrade_url": snapshot.upgrade_url},
        )
    if limit_key:
        limit = snapshot.limits.get(limit_key)
        usage_key = limit_key.removeprefix("limit.")
        usage = snapshot.usage.get(usage_key, 0)
        if limit is not None and usage >= limit:
            return tool_error(
                ErrorCode.PLAN_LIMIT_EXCEEDED,
                "Workspace plan limit exceeded",
                retryable=False,
                details={
                    "limit_key": limit_key,
                    "current_usage": usage,
                    "limit": limit,
                    "upgrade_url": snapshot.upgrade_url,
                },
            )
    return None
