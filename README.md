# incidentflow-mcp

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

The IncidentFlow MCP server is open-source under the MIT License.

IncidentFlow Cloud platform and hosted services are proprietary.

## VS Code MCP installer

After deploying this service behind an ingress (for example, `https://mcp.incidentflow.io`),
the app exposes a dynamic installer endpoint:

```bash
curl -fsSL https://incidentflow.io/install.sh | bash
```

The script auto-configures VS Code workspace `.vscode/mcp.json` with:
- `type: "http"`
- `url: https://<your-ingress-host>/mcp`

### Inspect the installer first (recommended)

You can inspect the installer before running it:

```bash
curl https://mcp.incidentflow.io/install.sh
```

or:

```bash
curl https://mcp.incidentflow.io/install.sh | less
```

### Dry run

Preview the changes without modifying your workspace:

```bash
curl https://mcp.incidentflow.io/install.sh | bash -s -- --dry-run
```

This prints the MCP configuration that would be written.
