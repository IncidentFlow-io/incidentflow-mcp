# Releasing IncidentFlow MCP

Production releases are created manually from annotated Git tags. The tag name
uses the deployment prefix; the version in `CHANGELOG.md` does not.

1. Add user-visible changes to `CHANGELOG.md` under `## [Unreleased]`.
2. Before tagging, rename that section to `## [X.Y.Z] - YYYY-MM-DD` and add a
   new empty `## [Unreleased]` section above it.
3. Merge the changelog commit to the production branch.
4. Create and push the tag from that exact commit:

   ```bash
   git tag -a incidentflow-mcp/vX.Y.Z -m "IncidentFlow MCP X.Y.Z"
   git push origin incidentflow-mcp/vX.Y.Z
   ```

Development tags use `incidentflow-mcp/dev-vX.Y.Z`. They do not receive their
own changelog entries; keep their user-visible changes under `Unreleased` until
the corresponding production release is cut.
