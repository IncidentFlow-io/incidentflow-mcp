<!-- incidentflow:internal-only -->

# Documentation governance

This repository separates public product documentation from internal engineering material with
the machine-readable `docs/docs-governance.json` manifest.

## Boundaries

- Only paths in `public_build_allowlist` may be included by Fern navigation or published.
- A document classified as `internal` must live under an `internal_roots` path and cannot appear
  in the public allow-list.
- Internal documents carry the `<!-- incidentflow:internal-only -->` marker. Public artifacts fail
  validation if they contain that marker, a managed internal path/document ID, or credential-shaped
  material.
- Every Markdown/MDX file under a `maintained_roots` path needs one unique manifest entry.
- Generated OpenAPI is public only when its code mapping and committed output are current.

## Change protocol

1. Change code and regenerate deterministic outputs with `make openapi-generate`.
2. Update the affected narrative, its semantic `version`, and review due date.
3. Run `make docs-sync` to regenerate OpenAPI and update drifted source/content digests. The sync is
   idempotent, clears prior approval metadata, and cannot create a human-approved state. Use
   `python3 scripts/docs_governance.py snapshot --doc-id <DOC_ID>` when a read-only metadata
   candidate is preferable.
4. Run `make docs-all` and attach the rendered preview plus command evidence to the pull request.
   The contract gate compares runtime routes, the FastMCP tool surface, registry schemas, OpenAPI,
   navigation and catalog, and executes every public bash/Python example against offline fixtures.
5. A human CODEOWNER who did not author the change reviews the narrative/generated artifacts and
   evidence. That human records `status: human-approved`, their handle and timestamp, the approved
   version, source ref/digest, content digest, and a future `review_due_at` in the reviewed pull
   request.
6. Production and preview publication run `make docs-publish-check`. Every public artifact,
   including generated OpenAPI, must have current human approval. Missing, bot-authored, expired,
   pre-sync, version-stale, source-stale, or content-stale approval blocks publication.

Automation may generate specs and issue-ready drift evidence. It must not fill human approval
fields, approve a pull request, merge narrative changes, or publish them.

## Scheduled drift check

`.github/workflows/docs-drift.yml` runs daily at 05:17 UTC. It regenerates OpenAPI, validates the
manifest and public examples, uploads deterministic JSON/Markdown evidence plus a mechanical sync
patch, and creates or updates one issue per `doc_id` using a stable title. The artifact is suitable
for a generated-only remediation pull request, but the workflow intentionally has no
`contents: write` or pull-request permission and cannot approve or publish anything.

Run the same check locally without changing tracked files:

```bash
report_dir=$(mktemp -d)
python3 scripts/docs_governance.py report --report-dir "$report_dir"
```
