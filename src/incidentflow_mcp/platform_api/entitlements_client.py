"""Typed, short-lived client for Platform's workspace entitlement snapshot."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import ClassVar

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.metrics import record_entitlement_snapshot


class EntitlementsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    workspace_id: str
    access_state: str
    limits: dict[str, int | None]
    features: dict[str, bool]
    usage: dict[str, int]
    upgrade_url: str
    ttl_seconds: int

    @property
    def writable(self) -> bool:
        return self.access_state in {"free", "trial", "team", "team_canceling", "grace"}


class PlatformEntitlementsClient:
    """Caches only successful snapshots; failures never extend cached writes."""

    _cache: ClassVar[dict[str, tuple[float, EntitlementSnapshot]]] = {}
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get(
        self, workspace_id: str, *, allow_cached: bool = False
    ) -> EntitlementSnapshot:
        now = time.monotonic()
        cached = self._cache.get(workspace_id)
        if allow_cached and cached is not None and cached[0] > now:
            record_entitlement_snapshot(source="cache", result="hit")
            return cached[1]
        record_entitlement_snapshot(source="cache", result="miss")
        if (
            not self._settings.platform_api_base_url
            or not self._settings.platform_api_internal_api_key
        ):
            raise EntitlementsUnavailable("Platform entitlement service is not configured")
        base_url = self._settings.platform_api_base_url.rstrip("/")
        url = f"{base_url}/internal/mcp/workspaces/{workspace_id}/entitlements"
        headers = {
            "X-Internal-Api-Key": self._settings.platform_api_internal_api_key.get_secret_value()
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.platform_api_timeout_seconds
            ) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            body = response.json()
            snapshot = EntitlementSnapshot(
                workspace_id=str(body["workspace_id"]),
                access_state=str(body["access_state"]),
                limits=dict(body.get("limits") or {}),
                features=dict(body.get("features") or {}),
                usage=dict(body.get("usage") or {}),
                upgrade_url=str(body.get("upgrade_url") or ""),
                ttl_seconds=min(60, max(1, int(body.get("ttl_seconds", 60)))),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            record_entitlement_snapshot(source="platform_api", result="unavailable")
            raise EntitlementsUnavailable("Platform entitlements are unavailable") from exc
        async with self._lock:
            self._cache[workspace_id] = (now + snapshot.ttl_seconds, snapshot)
        record_entitlement_snapshot(source="platform_api", result="success")
        return snapshot
