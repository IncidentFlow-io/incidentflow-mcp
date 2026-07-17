"""Typed knowledge-memory tools — dedicated upsert/find per document type.

Thin wrappers over PlatformAPIMemoryClient that preset the document `type` and map
tool-friendly ids (e.g. runbook_id) onto the platform-api upsert contract. The
backend derives a deterministic point id from (workspace, type, id), so re-saving
the same id updates the record instead of creating a duplicate.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.tools.memory_tools import MemoryAPIError, PlatformAPIMemoryClient

# Legacy `source` value written alongside each explicit type for backward compatibility.
_TYPE_TO_SOURCE = {
    "runbook": "runbook",
    "rca": "rca",
    "postmortem": "postmortem",
    "knowledge": "knowledge",
    "incident": "incident_summary",
}


def _slugify(text: str, *, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:80] or "untitled"
    return f"{prefix}-{slug}"


def _looks_like_markdown(text: str) -> bool:
    stripped = text.lstrip()
    return bool(
        stripped.startswith("# ")
        or stripped.startswith("## ")
        or stripped.startswith("```")
        or re.search(r"(?m)^\s*[-*]\s+\S", stripped)
        or re.search(r"(?m)^\s*\d+\.\s+\S", stripped)
    )


def _normalize_knowledge_markdown(*, title: str, text: str) -> str:
    normalized = text.strip()
    if not normalized or _looks_like_markdown(normalized):
        return normalized

    paragraphs = [
        " ".join(line.strip() for line in block.splitlines() if line.strip())
        for block in re.split(r"\n\s*\n", normalized)
    ]
    body = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    return f"# {title.strip() or 'Knowledge'}\n\n{body}"


def _ensure_markdown_tag(tags: list[str] | None) -> list[str]:
    ordered = list(tags or [])
    if "markdown" not in ordered:
        ordered.append("markdown")
    return ordered


async def _upsert_doc(
    settings: Settings,
    *,
    doc_type: str,
    workspace_id: str,
    external_id: str,
    title: str,
    text: str,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    created_by: str | None = None,
    started_at: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    try:
        result = await client.upsert(
            workspace_id=workspace_id,
            incident_id=external_id,
            document_id=external_id,
            source=_TYPE_TO_SOURCE.get(doc_type, doc_type),
            text=text,
            type=doc_type,
            title=title,
            service=service,
            cluster=cluster,
            namespace=namespace,
            severity=severity,
            status=status,
            tags=tags,
            metadata=metadata,
            created_by=created_by,
            started_at=started_at,
            dry_run=dry_run,
        )
        if dry_run:
            return {
                "stored": False,
                "dry_run": True,
                "validated": bool(result.get("validated")),
                "type": doc_type,
                "id": external_id,
                "point_id": None,
                "would_write": result.get("would_write"),
            }
        return {
            "stored": True,
            "type": doc_type,
            "id": external_id,
            "title": title,
            "point_id": result.get("point_id"),
            "text_hash": result.get("text_hash"),
        }
    except httpx.HTTPStatusError as exc:
        raise MemoryAPIError(f"{doc_type} upsert failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise MemoryAPIError(f"{doc_type} upsert error: {exc}") from exc


# ──────────────────────────────────────────────
# Write tools
# ──────────────────────────────────────────────


async def memory_upsert_runbook(
    settings: Settings,
    workspace_id: str,
    title: str,
    text: str,
    runbook_id: str | None = None,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    status: str = "active",
    dry_run: bool = False,
) -> dict[str, Any]:
    external_id = runbook_id or _slugify(title, prefix="runbook")
    return await _upsert_doc(
        settings,
        doc_type="runbook",
        workspace_id=workspace_id,
        external_id=external_id,
        title=title,
        text=text,
        service=service,
        cluster=cluster,
        namespace=namespace,
        severity=severity,
        status=status,
        tags=tags,
        dry_run=dry_run,
    )


async def memory_upsert_rca(
    settings: Settings,
    workspace_id: str,
    title: str,
    text: str,
    incident_id: str | None = None,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    external_id = incident_id or _slugify(title, prefix="rca")
    return await _upsert_doc(
        settings,
        doc_type="rca",
        workspace_id=workspace_id,
        external_id=external_id,
        title=title,
        text=text,
        service=service,
        cluster=cluster,
        namespace=namespace,
        severity=severity,
        tags=tags,
        dry_run=dry_run,
    )


async def memory_upsert_postmortem(
    settings: Settings,
    workspace_id: str,
    title: str,
    text: str,
    incident_id: str | None = None,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    external_id = incident_id or _slugify(title, prefix="postmortem")
    return await _upsert_doc(
        settings,
        doc_type="postmortem",
        workspace_id=workspace_id,
        external_id=external_id,
        title=title,
        text=text,
        service=service,
        cluster=cluster,
        namespace=namespace,
        severity=severity,
        tags=tags,
        dry_run=dry_run,
    )


async def memory_upsert_knowledge(
    settings: Settings,
    workspace_id: str,
    title: str,
    text: str,
    knowledge_id: str | None = None,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    external_id = knowledge_id or _slugify(title, prefix="knowledge")
    markdown_text = _normalize_knowledge_markdown(title=title, text=text)
    return await _upsert_doc(
        settings,
        doc_type="knowledge",
        workspace_id=workspace_id,
        external_id=external_id,
        title=title,
        text=markdown_text,
        service=service,
        cluster=cluster,
        namespace=namespace,
        tags=_ensure_markdown_tag(tags),
        dry_run=dry_run,
    )


async def memory_upsert_incident(
    settings: Settings,
    workspace_id: str,
    incident_id: str,
    title: str,
    text: str,
    service: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    started_at: str | None = None,
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    return await _upsert_doc(
        settings,
        doc_type="incident",
        workspace_id=workspace_id,
        external_id=incident_id,
        title=title,
        text=text,
        service=service,
        cluster=cluster,
        namespace=namespace,
        severity=severity,
        status=status,
        started_at=started_at,
        tags=tags,
        dry_run=dry_run,
    )


# ──────────────────────────────────────────────
# Read tools
# ──────────────────────────────────────────────


async def _find(
    settings: Settings,
    *,
    doc_type: str,
    result_key: str,
    workspace_id: str,
    query: str,
    service: str | None = None,
    tags: list[str] | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    client = PlatformAPIMemoryClient(settings)
    try:
        result = await client.search(
            workspace_id=workspace_id,
            query=query,
            service=service,
            tags=tags,
            types=[doc_type],
            exclude_status=["archived"],
            include_text=True,
            limit=limit,
        )
        matches = result.get("matches", [])
        return {
            "query": query,
            f"total_{result_key}": len(matches),
            result_key: matches,
        }
    except httpx.HTTPStatusError as exc:
        raise MemoryAPIError(f"{doc_type} search failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise MemoryAPIError(f"{doc_type} search error: {exc}") from exc


async def memory_find_rca(
    settings: Settings,
    workspace_id: str,
    query: str,
    service: str | None = None,
    tags: list[str] | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    return await _find(
        settings,
        doc_type="rca",
        result_key="rcas",
        workspace_id=workspace_id,
        query=query,
        service=service,
        tags=tags,
        limit=limit,
    )


async def memory_find_knowledge(
    settings: Settings,
    workspace_id: str,
    query: str,
    service: str | None = None,
    tags: list[str] | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    return await _find(
        settings,
        doc_type="knowledge",
        result_key="knowledge",
        workspace_id=workspace_id,
        query=query,
        service=service,
        tags=tags,
        limit=limit,
    )
