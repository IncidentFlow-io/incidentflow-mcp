"""Best-effort semantic memory helpers for MCP tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from pydantic import BaseModel

from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.memory_tools import PlatformAPIMemoryClient, memory_consult

logger = logging.getLogger(__name__)

WorkspaceResolver = Callable[[str | None], str]
TokenWorkspaceResolver = Callable[[], str | None]

_background_tasks: set[asyncio.Task[None]] = set()


def spawn_background_task(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class MemoryContextService:
    def __init__(
        self,
        settings: Settings,
        *,
        resolve_workspace_id: WorkspaceResolver,
        current_token_workspace_id: TokenWorkspaceResolver,
    ) -> None:
        self._settings = settings
        self._resolve_workspace_id = resolve_workspace_id
        self._current_token_workspace_id = current_token_workspace_id

    async def auto_upsert_thread_summary(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        thread_ts: str,
        result: dict[str, Any],
        alert_context: BaseModel | None,
    ) -> None:
        """Fire-and-forget: embed Slack thread summary into semantic memory."""
        try:
            parts: list[str] = []
            if title := result.get("title"):
                parts.append(str(title))
            if summary := result.get("summary"):
                parts.append(str(summary))
            if rca := result.get("probable_root_cause"):
                parts.append(f"Root cause: {rca}")
            if actions := result.get("actions_taken"):
                if isinstance(actions, list) and actions:
                    parts.append(f"Actions: {', '.join(str(a) for a in actions[:5])}")

            text = ". ".join(filter(None, parts)).strip()
            if not text:
                return

            incident_id = f"slack:{channel_id}:{thread_ts}"
            service = getattr(alert_context, "service", None) if alert_context else None
            severity = getattr(alert_context, "severity", None) if alert_context else None
            status = result.get("status")

            mem = PlatformAPIMemoryClient(self._settings)
            await mem.upsert(
                workspace_id=workspace_id,
                incident_id=incident_id,
                source="slack_thread",
                text=text,
                service=service,
                severity=severity,
                status=status,
            )
            logger.info(
                "memory: auto-upserted slack thread workspace=%s incident=%s service=%s",
                workspace_id,
                incident_id,
                service,
            )
        except Exception:
            logger.warning("memory: failed to auto-upsert thread summary", exc_info=True)

    async def consult_memory(
        self,
        *,
        query: str,
        service: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        tags: list[str] | None = None,
        workspace_id: str | None = None,
        score_threshold: float = 0.55,
    ) -> dict[str, Any] | None:
        """Best-effort semantic memory lookup to enrich diagnostic tool responses."""
        if not self._settings.mcp_memory_consult_enabled:
            return None
        if not self._settings.platform_api_base_url:
            return None
        if not (query and query.strip()):
            return None
        try:
            wid = self._resolve_workspace_id(workspace_id)
        except ValueError:
            return None
        try:
            return await asyncio.wait_for(
                memory_consult(
                    self._settings,
                    wid,
                    query,
                    service=service,
                    cluster=cluster,
                    namespace=namespace,
                    tags=tags,
                    score_threshold=score_threshold,
                ),
                timeout=self._settings.platform_api_timeout_seconds,
            )
        except Exception:
            logger.warning("memory: consult failed", exc_info=True)
            return None

    async def consult_pod_memory(
        self,
        describe: dict[str, Any],
        *,
        pod: str,
        namespace: str,
    ) -> dict[str, Any] | None:
        """Shared consult for pod-describe results."""
        data = describe.get("data") or {}
        diagnosis = data.get("diagnosis") or {}
        status = data.get("status") or {}
        issue_types = [
            str(i.get("type"))
            for i in (diagnosis.get("current_issues") or [])
            if isinstance(i, dict) and i.get("type")
        ]
        not_ready = not bool(status.get("ready"))
        if not (issue_types or not_ready):
            return None
        query = " ".join([*issue_types, pod, namespace]).strip() or f"{pod} {namespace}"
        return await self.consult_memory(query=query, namespace=namespace)


MemoryConsult = Callable[..., Awaitable[dict[str, Any] | None]]
