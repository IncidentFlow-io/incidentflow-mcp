#!/usr/bin/env python3
"""Validate and mechanically sync IncidentFlow documentation provenance.

The manifest is deliberately JSON so this release gate has no dependency on the application
environment. ``sync`` may refresh drifted digests and clear approval metadata; no mode can create
human approval, update narrative content, open a pull request, or publish a site.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED_VISIBILITY = {"public", "internal"}
ALLOWED_STATUS = {"draft", "automated-update", "human-approved", "stale", "retired"}
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAV_PATH_RE = re.compile(r"^\s*path:\s*([^\s#]+)", re.MULTILINE)
BOT_REVIEWER_RE = re.compile(r"(?:\[bot\]|github-actions|automation)", re.IGNORECASE)
PUBLIC_SECRET_PATTERNS = (
    re.compile(r"\bif_pat_[A-Za-z0-9_-]+"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]+"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
)
APPROVAL_METADATA_FIELDS = (
    "approved_version",
    "approved_source_ref",
    "approved_source_digest",
    "approved_content_digest",
    "last_human_reviewed_at",
    "last_human_reviewer",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    doc_id: str = "manifest"

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "doc_id": self.doc_id,
            "message": self.message,
        }


def _parse_time(value: object, field: str, doc_id: str, findings: list[Finding]) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        findings.append(
            Finding(
                "error", "INVALID_TIMESTAMP", f"{field} must be an ISO-8601 string or null", doc_id
            )
        )
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        findings.append(
            Finding("error", "INVALID_TIMESTAMP", f"{field} is not valid ISO-8601: {value}", doc_id)
        )
        return None
    if parsed.tzinfo is None:
        findings.append(
            Finding("error", "INVALID_TIMESTAMP", f"{field} must include a timezone", doc_id)
        )
        return None
    return parsed.astimezone(UTC)


def _safe_relative_path(
    value: object, field: str, doc_id: str, findings: list[Finding]
) -> str | None:
    if not isinstance(value, str) or not value:
        findings.append(
            Finding("error", "INVALID_PATH", f"{field} must be a non-empty relative path", doc_id)
        )
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        findings.append(
            Finding("error", "INVALID_PATH", f"{field} escapes the repository: {value}", doc_id)
        )
        return None
    return path.as_posix()


def _matches_root(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _expand_sources(root: Path, patterns: list[str]) -> list[Path]:
    matches: dict[str, Path] = {}
    for pattern in patterns:
        for candidate in root.glob(pattern):
            if candidate.is_file() and ".git" not in candidate.parts:
                matches[candidate.relative_to(root).as_posix()] = candidate
    return [matches[key] for key in sorted(matches)]


def _digest_files(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _digest_path(root: Path, relative: str) -> str | None:
    path = root / relative
    return _digest_files(root, [path]) if path.is_file() else None


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read documentation manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"documentation manifest {path} must contain a JSON object")
    return data


def validate_manifest(
    root: Path, manifest: dict[str, Any], *, publish: bool = False
) -> list[Finding]:
    findings: list[Finding] = []
    now = datetime.now(UTC)

    if manifest.get("schema_version") != 1:
        findings.append(Finding("error", "SCHEMA_VERSION", "schema_version must be 1"))

    raw_roots = manifest.get("maintained_roots", [])
    raw_public_roots = manifest.get("public_roots", [])
    raw_internal_roots = manifest.get("internal_roots", [])
    raw_allowlist = manifest.get("public_build_allowlist", [])
    raw_internal_markers = manifest.get("internal_content_markers", [])
    if not all(
        isinstance(items, list)
        for items in (
            raw_roots,
            raw_public_roots,
            raw_internal_roots,
            raw_allowlist,
            raw_internal_markers,
        )
    ):
        findings.append(
            Finding(
                "error",
                "INVALID_MANIFEST",
                "roots, public_build_allowlist and internal_content_markers must be arrays",
            )
        )
        return findings

    maintained_roots = [str(item).rstrip("/") for item in raw_roots]
    public_roots = [str(item).rstrip("/") for item in raw_public_roots]
    internal_roots = [str(item).rstrip("/") for item in raw_internal_roots]
    allowlist = [str(item) for item in raw_allowlist]
    internal_markers = [str(item) for item in raw_internal_markers]
    if any(not marker for marker in internal_markers):
        findings.append(
            Finding(
                "error",
                "INVALID_INTERNAL_MARKER",
                "internal_content_markers must contain non-empty strings",
            )
        )
    if len(allowlist) != len(set(allowlist)):
        findings.append(
            Finding("error", "DUPLICATE_ALLOWLIST", "public_build_allowlist contains duplicates")
        )

    documents = manifest.get("documents")
    if not isinstance(documents, list):
        findings.append(Finding("error", "INVALID_MANIFEST", "documents must be an array"))
        return findings

    paths: dict[str, str] = {}
    doc_ids: set[str] = set()
    public_documents: list[tuple[str, str, dict[str, Any]]] = []
    internal_documents: list[tuple[str, str]] = []
    for raw_doc in documents:
        if not isinstance(raw_doc, dict):
            findings.append(
                Finding("error", "INVALID_DOCUMENT", "each documents item must be an object")
            )
            continue
        doc_id = raw_doc.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            findings.append(Finding("error", "MISSING_DOC_ID", "document is missing doc_id"))
            continue
        if doc_id in doc_ids:
            findings.append(
                Finding("error", "DUPLICATE_DOC_ID", f"duplicate doc_id: {doc_id}", doc_id)
            )
        doc_ids.add(doc_id)

        relative = _safe_relative_path(raw_doc.get("path"), "path", doc_id, findings)
        if relative is None:
            continue
        if relative in paths:
            findings.append(
                Finding(
                    "error",
                    "DUPLICATE_PATH",
                    f"path is also owned by {paths[relative]}: {relative}",
                    doc_id,
                )
            )
        paths[relative] = doc_id
        path = root / relative
        if not path.is_file():
            findings.append(
                Finding("error", "MISSING_DOCUMENT", f"document does not exist: {relative}", doc_id)
            )

        visibility = raw_doc.get("visibility")
        if visibility not in ALLOWED_VISIBILITY:
            findings.append(
                Finding(
                    "error",
                    "INVALID_VISIBILITY",
                    f"visibility must be one of {sorted(ALLOWED_VISIBILITY)}",
                    doc_id,
                )
            )
        if visibility == "public" and not _matches_root(relative, public_roots):
            findings.append(
                Finding(
                    "error",
                    "PUBLIC_PATH_BOUNDARY",
                    f"public document is outside public_roots: {relative}",
                    doc_id,
                )
            )
        if visibility == "internal" and not _matches_root(relative, internal_roots):
            findings.append(
                Finding(
                    "error",
                    "INTERNAL_PATH_BOUNDARY",
                    f"internal document is outside internal_roots: {relative}",
                    doc_id,
                )
            )
        if visibility == "internal" and relative in allowlist:
            findings.append(
                Finding(
                    "error",
                    "INTERNAL_PUBLICATION",
                    f"internal document is in the public allow-list: {relative}",
                    doc_id,
                )
            )
        if visibility == "public":
            public_documents.append((doc_id, relative, raw_doc))
        elif visibility == "internal":
            internal_documents.append((doc_id, relative))

        published = raw_doc.get("published")
        if not isinstance(published, bool):
            findings.append(
                Finding("error", "INVALID_PUBLISHED", "published must be a boolean", doc_id)
            )
        if published is True and (visibility != "public" or relative not in allowlist):
            findings.append(
                Finding(
                    "error",
                    "PUBLIC_ALLOWLIST",
                    f"published document must be public and allow-listed: {relative}",
                    doc_id,
                )
            )
        if relative in allowlist and published is not True:
            findings.append(
                Finding(
                    "error",
                    "PUBLIC_ALLOWLIST",
                    f"allow-listed document must set published=true: {relative}",
                    doc_id,
                )
            )

        version = raw_doc.get("version")
        if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
            findings.append(
                Finding(
                    "error",
                    "INVALID_VERSION",
                    f"version must be MAJOR.MINOR.PATCH: {version}",
                    doc_id,
                )
            )
        status = raw_doc.get("status")
        if status not in ALLOWED_STATUS:
            findings.append(
                Finding(
                    "error",
                    "INVALID_STATUS",
                    f"status must be one of {sorted(ALLOWED_STATUS)}",
                    doc_id,
                )
            )
        owners = raw_doc.get("owners")
        if (
            not isinstance(owners, list)
            or not owners
            or not all(isinstance(owner, str) and owner for owner in owners)
        ):
            findings.append(
                Finding(
                    "error", "INVALID_OWNERS", "owners must be a non-empty string array", doc_id
                )
            )

        source_ref = raw_doc.get("source_ref")
        if not isinstance(source_ref, str) or SHA_RE.fullmatch(source_ref) is None:
            findings.append(
                Finding(
                    "error",
                    "INVALID_SOURCE_REF",
                    "source_ref must be a full lowercase Git SHA",
                    doc_id,
                )
            )
        source_paths = raw_doc.get("source_paths")
        if (
            not isinstance(source_paths, list)
            or not source_paths
            or not all(isinstance(item, str) and item for item in source_paths)
        ):
            findings.append(
                Finding(
                    "error",
                    "INVALID_SOURCE_PATHS",
                    "source_paths must be a non-empty string array",
                    doc_id,
                )
            )
            source_paths = []
        else:
            for pattern in source_paths:
                _safe_relative_path(pattern, "source_paths item", doc_id, findings)
        source_files = _expand_sources(root, source_paths)
        if not source_files:
            findings.append(
                Finding(
                    "error",
                    "EMPTY_SOURCE_MAPPING",
                    f"source_paths match no files: {source_paths}",
                    doc_id,
                )
            )
        actual_source_digest = _digest_files(root, source_files)
        if raw_doc.get("source_digest") != actual_source_digest:
            code = "APPROVAL_INVALID" if status == "human-approved" else "SOURCE_DRIFT"
            findings.append(
                Finding(
                    "error",
                    code,
                    f"mapped code/schema changed; expected source_digest {actual_source_digest}",
                    doc_id,
                )
            )

        actual_content_digest = _digest_path(root, relative)
        if (
            actual_content_digest is not None
            and raw_doc.get("content_digest") != actual_content_digest
        ):
            code = "APPROVAL_INVALID" if status == "human-approved" else "CONTENT_DRIFT"
            findings.append(
                Finding(
                    "error",
                    code,
                    f"document content changed; expected content_digest {actual_content_digest}",
                    doc_id,
                )
            )

        code_sync_at = _parse_time(
            raw_doc.get("last_code_sync_at"), "last_code_sync_at", doc_id, findings
        )
        if code_sync_at is None:
            findings.append(
                Finding(
                    "error",
                    "MISSING_CODE_SYNC",
                    "last_code_sync_at is required",
                    doc_id,
                )
            )
        reviewed_at = _parse_time(
            raw_doc.get("last_human_reviewed_at"), "last_human_reviewed_at", doc_id, findings
        )
        due_at = _parse_time(raw_doc.get("review_due_at"), "review_due_at", doc_id, findings)
        reviewer = raw_doc.get("last_human_reviewer")
        approved_version = raw_doc.get("approved_version")
        approved_source_ref = raw_doc.get("approved_source_ref")
        approved_source_digest = raw_doc.get("approved_source_digest")
        approved_digest = raw_doc.get("approved_content_digest")
        if status == "human-approved":
            if not isinstance(reviewer, str) or not reviewer or reviewed_at is None:
                findings.append(
                    Finding(
                        "error",
                        "MISSING_HUMAN_APPROVAL",
                        "human-approved requires reviewer and review timestamp",
                        doc_id,
                    )
                )
            elif BOT_REVIEWER_RE.search(reviewer):
                findings.append(
                    Finding(
                        "error",
                        "HUMAN_REVIEWER_REQUIRED",
                        "approval reviewer must identify a human, not automation",
                        doc_id,
                    )
                )
            if reviewed_at is not None and reviewed_at > now:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_TIMESTAMP_FUTURE",
                        "human review timestamp cannot be in the future",
                        doc_id,
                    )
                )
            if code_sync_at is not None and reviewed_at is not None and reviewed_at < code_sync_at:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_BEFORE_CODE_SYNC",
                        "human review must occur after the recorded code/document sync",
                        doc_id,
                    )
                )
            if due_at is None:
                findings.append(
                    Finding(
                        "error",
                        "MISSING_REVIEW_DUE",
                        "human-approved documentation requires review_due_at",
                        doc_id,
                    )
                )
            elif reviewed_at is not None and due_at <= reviewed_at:
                findings.append(
                    Finding(
                        "error",
                        "INVALID_REVIEW_WINDOW",
                        "review_due_at must be later than last_human_reviewed_at",
                        doc_id,
                    )
                )
            if approved_version != version:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_INVALID",
                        "approved_version does not match the current document version",
                        doc_id,
                    )
                )
            if approved_source_ref != source_ref:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_INVALID",
                        "approved_source_ref does not match the current source_ref",
                        doc_id,
                    )
                )
            if approved_source_digest != actual_source_digest:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_INVALID",
                        "approved_source_digest does not match mapped code/schema",
                        doc_id,
                    )
                )
            if approved_digest != actual_content_digest:
                findings.append(
                    Finding(
                        "error",
                        "APPROVAL_INVALID",
                        "approved_content_digest does not match current content",
                        doc_id,
                    )
                )
        elif any(raw_doc.get(field) is not None for field in APPROVAL_METADATA_FIELDS):
            findings.append(
                Finding(
                    "error",
                    "STALE_APPROVAL_METADATA",
                    "non-approved status must clear all reviewer and approved-version/digest data",
                    doc_id,
                )
            )
        if due_at is not None and due_at < now and status != "retired":
            severity = "error" if visibility == "public" else "warning"
            findings.append(
                Finding(
                    severity,
                    "REVIEW_OVERDUE",
                    f"human review expired at {raw_doc.get('review_due_at')}",
                    doc_id,
                )
            )

        if publish and published is True:
            if status != "human-approved":
                findings.append(
                    Finding(
                        "error",
                        "PUBLISH_REQUIRES_HUMAN",
                        "every public artifact requires status=human-approved",
                        doc_id,
                    )
                )

    internal_references = {
        reference
        for doc_id, relative in internal_documents
        for reference in (doc_id, relative)
    }
    for doc_id, relative, raw_doc in public_documents:
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if path.suffix == ".mdx":
            expected_version = f"Contract version: `{raw_doc.get('version')}`"
            expected_review = (
                "Human documentation review: approved"
                if raw_doc.get("status") == "human-approved"
                else "Human documentation review: pending"
            )
            if expected_version not in content or expected_review not in content:
                findings.append(
                    Finding(
                        "error",
                        "PUBLIC_METADATA_MISMATCH",
                        "embedded version/review metadata does not match the manifest",
                        doc_id,
                    )
                )
        leaked_references = sorted(
            reference for reference in internal_references if reference and reference in content
        )
        leaked_markers = sorted(marker for marker in internal_markers if marker in content)
        if leaked_references or leaked_markers:
            leaked = leaked_references + leaked_markers
            findings.append(
                Finding(
                    "error",
                    "PUBLIC_INTERNAL_LEAKAGE",
                    f"public artifact contains internal-only reference/marker: {leaked[0]}",
                    doc_id,
                )
            )
        for pattern in PUBLIC_SECRET_PATTERNS:
            if pattern.search(content):
                findings.append(
                    Finding(
                        "error",
                        "PUBLIC_SECRET_MATERIAL",
                        f"public artifact matches forbidden credential pattern: {pattern.pattern}",
                        doc_id,
                    )
                )
                break

    for allowlisted in allowlist:
        safe = _safe_relative_path(allowlisted, "public_build_allowlist item", "manifest", findings)
        if safe is None:
            continue
        if safe not in paths:
            findings.append(
                Finding(
                    "error",
                    "UNMANAGED_PUBLIC_DOCUMENT",
                    f"allow-listed path has no metadata entry: {safe}",
                )
            )
        if _matches_root(safe, internal_roots):
            findings.append(
                Finding(
                    "error", "INTERNAL_PUBLICATION", f"internal-root path is allow-listed: {safe}"
                )
            )

    for maintained_root in maintained_roots:
        directory = root / maintained_root
        if not directory.exists():
            findings.append(
                Finding(
                    "error",
                    "MISSING_MAINTAINED_ROOT",
                    f"maintained root does not exist: {maintained_root}",
                )
            )
            continue
        for extension in ("*.md", "*.mdx"):
            for discovered in sorted(directory.rglob(extension)):
                relative = discovered.relative_to(root).as_posix()
                if relative not in paths:
                    findings.append(
                        Finding(
                            "error",
                            "UNMANAGED_DOCUMENT",
                            f"maintained document has no metadata entry: {relative}",
                        )
                    )

    for navigation_file in manifest.get("navigation_files", []):
        relative_nav = _safe_relative_path(
            navigation_file, "navigation_files item", "manifest", findings
        )
        if relative_nav is None:
            continue
        nav_path = root / relative_nav
        if not nav_path.is_file():
            findings.append(
                Finding(
                    "error", "MISSING_NAVIGATION", f"navigation file does not exist: {relative_nav}"
                )
            )
            continue
        for match in NAV_PATH_RE.findall(nav_path.read_text(encoding="utf-8")):
            resolved = (Path(relative_nav).parent / match.strip("\"'")).as_posix()
            if resolved not in allowlist:
                findings.append(
                    Finding(
                        "error",
                        "NAVIGATION_NOT_ALLOWLISTED",
                        f"navigation references non-allow-listed path: {resolved}",
                    )
                )

    return sorted(findings, key=lambda item: (item.doc_id, item.severity, item.code, item.message))


def _write_report(report_dir: Path, manifest: dict[str, Any], findings: list[Finding]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    repository = str(manifest.get("repository", "unknown-repository"))
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.doc_id, []).append(finding)
    issues = []
    for doc_id, doc_findings in sorted(grouped.items()):
        body_lines = [
            f"<!-- docs-drift-key:{repository}:{doc_id} -->",
            f"Documentation governance found drift for `{doc_id}` in `{repository}`.",
            "",
            "| Severity | Code | Finding |",
            "| --- | --- | --- |",
        ]
        for finding in doc_findings:
            escaped = finding.message.replace("|", "\\|").replace("\n", " ")
            body_lines.append(f"| {finding.severity} | `{finding.code}` | {escaped} |")
        body_lines.extend(
            [
                "",
                "Generated automatically. A human CODEOWNER must review narrative changes "
                "and record approval; automation must not approve or publish them.",
            ]
        )
        issues.append(
            {
                "dedup_key": f"{repository}:{doc_id}",
                "doc_id": doc_id,
                "title": f"[docs-drift][{doc_id}] Documentation contract drift",
                "body": "\n".join(body_lines),
            }
        )

    payload = {
        "schema_version": 1,
        "repository": repository,
        "ok": not any(item.severity == "error" for item in findings),
        "findings": [item.as_dict() for item in findings],
        "issues": issues,
    }
    (report_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    summary = [f"# Documentation drift report: {repository}", ""]
    if not findings:
        summary.append("No drift detected.")
    else:
        for issue in issues:
            summary.extend([f"## {issue['doc_id']}", "", issue["body"], ""])
    (report_dir / "report.md").write_text("\n".join(summary).rstrip() + "\n", encoding="utf-8")


def _git_head(root: Path) -> str:
    try:
        source_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"cannot resolve git HEAD for documentation metadata: {exc}") from exc
    if SHA_RE.fullmatch(source_ref) is None:
        raise SystemExit(f"git HEAD is not a full lowercase SHA: {source_ref}")
    return source_ref


def _sync_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    selected_doc_ids: list[str],
    source_ref: str,
    synced_at: str,
) -> list[str]:
    """Update drifted digests while always invalidating prior human approval.

    This is intentionally mechanical. It cannot create a human-approved state and it leaves
    unchanged documents untouched so a second sync over the same tree is a byte-for-byte no-op.
    """
    if SHA_RE.fullmatch(source_ref) is None:
        raise ValueError("source_ref must be a full lowercase Git SHA")
    selected = set(selected_doc_ids)
    changed: list[str] = []
    seen: set[str] = set()
    documents = manifest.get("documents", [])
    if not isinstance(documents, list):
        raise ValueError("documents must be an array")
    for raw_doc in documents:
        if not isinstance(raw_doc, dict):
            continue
        doc_id = raw_doc.get("doc_id")
        if not isinstance(doc_id, str):
            continue
        seen.add(doc_id)
        if selected and doc_id not in selected:
            continue
        source_paths = raw_doc.get("source_paths", [])
        relative = raw_doc.get("path")
        if not isinstance(source_paths, list) or not isinstance(relative, str):
            continue
        source_digest = _digest_files(root, _expand_sources(root, source_paths))
        content_digest = _digest_path(root, relative)
        drifted = (
            raw_doc.get("source_digest") != source_digest
            or raw_doc.get("content_digest") != content_digest
            or any(field not in raw_doc for field in APPROVAL_METADATA_FIELDS)
        )
        if not drifted:
            continue

        raw_doc["source_ref"] = source_ref
        raw_doc["source_digest"] = source_digest
        raw_doc["content_digest"] = content_digest
        raw_doc["last_code_sync_at"] = synced_at
        if raw_doc.get("status") == "human-approved":
            raw_doc["status"] = "automated-update" if raw_doc.get("published") else "draft"
        for field in APPROVAL_METADATA_FIELDS:
            raw_doc[field] = None
        changed.append(doc_id)

    missing = selected - seen
    if missing:
        raise ValueError(f"unknown doc_id(s): {', '.join(sorted(missing))}")
    return sorted(changed)


def _print_snapshot(root: Path, manifest: dict[str, Any], selected_doc_ids: list[str]) -> None:
    """Print review metadata candidates without modifying or approving anything."""
    source_ref = _git_head(root)
    selected = set(selected_doc_ids)
    snapshots = []
    for doc in manifest.get("documents", []):
        doc_id = doc.get("doc_id")
        if selected and doc_id not in selected:
            continue
        relative = str(doc.get("path", ""))
        sources = doc.get("source_paths", [])
        snapshots.append(
            {
                "doc_id": doc_id,
                "source_ref": source_ref,
                "source_digest": _digest_files(root, _expand_sources(root, sources)),
                "content_digest": _digest_path(root, relative),
                "status": "automated-update",
                "approved_version": None,
                "approved_source_ref": None,
                "approved_source_digest": None,
                "approved_content_digest": None,
                "last_code_sync_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "last_human_reviewed_at": None,
                "last_human_reviewer": None,
            }
        )
    missing = selected - {str(item["doc_id"]) for item in snapshots}
    if missing:
        raise SystemExit(f"unknown doc_id(s): {', '.join(sorted(missing))}")
    print(json.dumps({"documents": snapshots}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("validate", "publish", "report", "snapshot", "sync"),
        nargs="?",
        default="validate",
    )
    parser.add_argument("--manifest", type=Path, default=Path("docs/docs-governance.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--report-dir", type=Path, default=Path("docs-drift-report"))
    parser.add_argument("--doc-id", action="append", default=[])
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    manifest = _load_manifest(manifest_path)
    if args.mode == "snapshot":
        _print_snapshot(root, manifest, args.doc_id)
        return 0
    if args.mode == "sync":
        synced_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            changed = _sync_manifest(
                root,
                manifest,
                selected_doc_ids=args.doc_id,
                source_ref=_git_head(root),
                synced_at=synced_at,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if changed:
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            print(f"documentation governance sync: updated {', '.join(changed)}")
        else:
            print("documentation governance sync: no drift")
        return 0
    findings = validate_manifest(root, manifest, publish=args.mode == "publish")
    if args.mode == "report":
        _write_report(args.report_dir, manifest, findings)
    for finding in findings:
        print(f"{finding.severity.upper()} [{finding.code}] {finding.doc_id}: {finding.message}")
    if not findings:
        print(f"documentation governance {args.mode}: ok")
    return 1 if any(item.severity == "error" for item in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
