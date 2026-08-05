from __future__ import annotations

import runpy
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[1]
MODULE = runpy.run_path(
    str(REPO_ROOT / "scripts" / "docs_governance.py"),
    run_name="docs_governance",
)
validate_manifest = MODULE["validate_manifest"]
digest_files = MODULE["_digest_files"]
digest_path = MODULE["_digest_path"]
write_report = MODULE["_write_report"]
sync_manifest = MODULE["_sync_manifest"]
Finding = MODULE["Finding"]


def _manifest(root: Path, *, status: str = "automated-update") -> dict[str, object]:
    (root / "public").mkdir()
    (root / "internal").mkdir()
    (root / "public" / "page.md").write_text("# Public\n", encoding="utf-8")
    (root / "internal" / "runbook.md").write_text("# Internal\n", encoding="utf-8")
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_digest = digest_files(root, [root / "source.py"])
    reviewed = "2026-01-02T00:00:00Z" if status == "human-approved" else None
    reviewer = "@reviewer" if status == "human-approved" else None
    approved = digest_path(root, "public/page.md") if status == "human-approved" else None
    approved_source = source_digest if status == "human-approved" else None
    approved_ref = "a" * 40 if status == "human-approved" else None
    approved_version = "1.0.0" if status == "human-approved" else None
    return {
        "schema_version": 1,
        "repository": "example/repository",
        "maintained_roots": ["public", "internal"],
        "public_roots": ["public"],
        "internal_roots": ["internal"],
        "public_build_allowlist": ["public/page.md"],
        "internal_content_markers": ["<!-- incidentflow:internal-only -->"],
        "navigation_files": [],
        "documents": [
            {
                "doc_id": "PUBLIC-PAGE",
                "path": "public/page.md",
                "visibility": "public",
                "published": True,
                "generated": False,
                "version": "1.0.0",
                "status": status,
                "owners": ["@owner"],
                "source_paths": ["source.py"],
                "source_ref": "a" * 40,
                "source_digest": source_digest,
                "content_digest": digest_path(root, "public/page.md"),
                "approved_version": approved_version,
                "approved_source_ref": approved_ref,
                "approved_source_digest": approved_source,
                "approved_content_digest": approved,
                "last_code_sync_at": "2026-01-01T00:00:00Z",
                "last_human_reviewed_at": reviewed,
                "last_human_reviewer": reviewer,
                "review_due_at": "2099-02-01T00:00:00Z",
            },
            {
                "doc_id": "INTERNAL-RUNBOOK",
                "path": "internal/runbook.md",
                "visibility": "internal",
                "published": False,
                "generated": False,
                "version": "1.0.0",
                "status": "draft",
                "owners": ["@owner"],
                "source_paths": ["source.py"],
                "source_ref": "a" * 40,
                "source_digest": source_digest,
                "content_digest": digest_path(root, "internal/runbook.md"),
                "approved_version": None,
                "approved_source_ref": None,
                "approved_source_digest": None,
                "approved_content_digest": None,
                "last_code_sync_at": "2026-01-01T00:00:00Z",
                "last_human_reviewed_at": None,
                "last_human_reviewer": None,
                "review_due_at": "2099-02-01T00:00:00Z",
            },
        ],
    }


def _codes(findings: list[object]) -> set[str]:
    return {finding.code for finding in findings}


def test_valid_manifest_separates_public_and_internal_docs(tmp_path: Path) -> None:
    assert validate_manifest(tmp_path, _manifest(tmp_path)) == []


def test_internal_document_cannot_enter_public_allowlist(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["public_build_allowlist"].append("internal/runbook.md")

    assert "INTERNAL_PUBLICATION" in _codes(validate_manifest(tmp_path, manifest))


def test_public_document_rejects_internal_reference_and_marker(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "public" / "page.md").write_text(
        "# Public\n\nSee internal/runbook.md.\n<!-- incidentflow:internal-only -->\n",
        encoding="utf-8",
    )
    manifest["documents"][0]["content_digest"] = digest_path(tmp_path, "public/page.md")

    assert "PUBLIC_INTERNAL_LEAKAGE" in _codes(validate_manifest(tmp_path, manifest))


def test_public_document_rejects_credential_shaped_content(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "public" / "page.md").write_text(
        "# Public\n\nNever publish xoxb-secret-shaped-value.\n",
        encoding="utf-8",
    )
    manifest["documents"][0]["content_digest"] = digest_path(tmp_path, "public/page.md")

    assert "PUBLIC_SECRET_MATERIAL" in _codes(validate_manifest(tmp_path, manifest))


def test_public_mdx_metadata_must_match_manifest_version_and_review_state(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    public = manifest["documents"][0]
    (tmp_path / "public" / "page.md").rename(tmp_path / "public" / "page.mdx")
    (tmp_path / "public" / "page.mdx").write_text(
        "# Public\n\n> Contract version: `9.9.9` · Human documentation review: approved\n",
        encoding="utf-8",
    )
    public["path"] = "public/page.mdx"
    public["content_digest"] = digest_path(tmp_path, "public/page.mdx")
    manifest["public_build_allowlist"] = ["public/page.mdx"]

    assert "PUBLIC_METADATA_MISMATCH" in _codes(validate_manifest(tmp_path, manifest))


def test_content_change_invalidates_human_approval(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, status="human-approved")
    (tmp_path / "public" / "page.md").write_text("# Changed without review\n", encoding="utf-8")

    assert "APPROVAL_INVALID" in _codes(validate_manifest(tmp_path, manifest))


def test_publish_rejects_unapproved_public_narrative(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    assert "PUBLISH_REQUIRES_HUMAN" in _codes(validate_manifest(tmp_path, manifest, publish=True))


def test_publish_rejects_unapproved_generated_artifact(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["documents"][0]["generated"] = True

    assert "PUBLISH_REQUIRES_HUMAN" in _codes(validate_manifest(tmp_path, manifest, publish=True))


def test_human_approval_is_bound_to_version_source_and_review_window(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, status="human-approved")
    public = manifest["documents"][0]
    public["version"] = "1.0.1"
    public["approved_source_digest"] = "sha256:" + "0" * 64
    public["review_due_at"] = None

    codes = _codes(validate_manifest(tmp_path, manifest, publish=True))
    assert "APPROVAL_INVALID" in codes
    assert "MISSING_REVIEW_DUE" in codes


def test_automation_identity_cannot_claim_human_approval(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, status="human-approved")
    manifest["documents"][0]["last_human_reviewer"] = "github-actions[bot]"

    assert "HUMAN_REVIEWER_REQUIRED" in _codes(validate_manifest(tmp_path, manifest))


def test_source_mapping_detects_code_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert "SOURCE_DRIFT" in _codes(validate_manifest(tmp_path, manifest))


def test_source_change_invalidates_human_approval(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, status="human-approved")
    (tmp_path / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert "APPROVAL_INVALID" in _codes(validate_manifest(tmp_path, manifest))


def test_sync_clears_approval_and_publish_remains_blocked_until_reapproval(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, status="human-approved")
    (tmp_path / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    changed = sync_manifest(
        tmp_path,
        manifest,
        selected_doc_ids=[],
        source_ref="b" * 40,
        synced_at="2026-01-03T00:00:00Z",
    )

    assert changed == ["INTERNAL-RUNBOOK", "PUBLIC-PAGE"]
    public = manifest["documents"][0]
    assert public["status"] == "automated-update"
    assert all(public[field] is None for field in MODULE["APPROVAL_METADATA_FIELDS"])
    assert validate_manifest(tmp_path, manifest) == []
    assert "PUBLISH_REQUIRES_HUMAN" in _codes(
        validate_manifest(tmp_path, manifest, publish=True)
    )

    public.update(
        {
            "status": "human-approved",
            "approved_version": public["version"],
            "approved_source_ref": public["source_ref"],
            "approved_source_digest": public["source_digest"],
            "approved_content_digest": public["content_digest"],
            "last_human_reviewed_at": "2026-01-04T00:00:00Z",
            "last_human_reviewer": "@independent-reviewer",
        }
    )
    assert validate_manifest(tmp_path, manifest, publish=True) == []


def test_sync_is_noop_when_digests_are_current(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    before = repr(manifest)

    assert (
        sync_manifest(
            tmp_path,
            manifest,
            selected_doc_ids=[],
            source_ref="b" * 40,
            synced_at="2026-01-03T00:00:00Z",
        )
        == []
    )
    assert repr(manifest) == before


def test_report_is_deterministic_and_deduplicated_by_doc_id(tmp_path: Path) -> None:
    manifest = {"repository": "example/repository"}
    findings = [
        Finding("error", "SOURCE_DRIFT", "source changed", "PUBLIC-PAGE"),
        Finding("error", "CONTENT_DRIFT", "content changed", "PUBLIC-PAGE"),
    ]
    first = tmp_path / "first"
    second = tmp_path / "second"

    write_report(first, manifest, findings)
    write_report(second, manifest, findings)

    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    assert (first / "report.md").read_bytes() == (second / "report.md").read_bytes()
    assert (first / "report.json").read_text().count('"dedup_key"') == 1


def test_docs_ci_runs_contracts_and_blocks_every_publish_job_on_human_gate() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8")
    )
    validate_runs = [step.get("run", "") for step in workflow["jobs"]["validate"]["steps"]]
    assert "make docs-contract-check" in validate_runs
    assert "make docs-governance-check" in validate_runs

    for job_name in ("publish-preview", "publish-production"):
        steps = workflow["jobs"][job_name]["steps"]
        gate_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("run") == "make docs-publish-check"
        )
        publish_index = next(
            index for index, step in enumerate(steps) if "fern-docs-" in step.get("run", "")
        )
        assert gate_index < publish_index
        assert steps[gate_index].get("continue-on-error") is not True
        assert workflow["jobs"][job_name]["needs"] == "validate"


def test_drift_cron_proposes_sync_but_has_no_publish_or_approval_permission() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/docs-drift.yml").read_text(encoding="utf-8")
    )
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    runs = [step.get("run", "") for step in workflow["jobs"]["drift"]["steps"]]
    joined = "\n".join(runs)
    assert "scripts/docs_governance.py report" in joined
    assert "scripts/docs_governance.py sync" in joined
    assert "scripts/docs_governance.py validate" in joined
    assert "scripts/docs_governance.py publish" not in joined
    assert "fern-docs-publish" not in joined
