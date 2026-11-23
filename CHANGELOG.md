# Changelog

All notable changes to IncidentFlow MCP are documented here.

This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Read-only Logs tools for source discovery, connection health, bounded queries,
  and contextual log retrieval.
- A Logs capability category and tool metadata for AI-client discovery.
- Platform-backed incident summary retrieval for MCP investigations.

### Changed

- Read-only MCP tools remain available when a workspace has diagnostic read
  access but write actions are restricted.

### Fixed

- Logs queries at the configured time-window limit no longer fail because of
  request transit time.

## [1.0.42] - 2026-11-06

### Added

- Investigation tools can return linked incident evidence for a requested
  investigation.

### Changed

- OAuth authorization responses include the configured authorization endpoint
  metadata.

## [1.0.41] - 2026-10-28

### Changed

- Tool actions report a machine-readable `POLICY_DENIED` result when workspace
  policy prevents execution.
- CI workflows use the current GitHub Actions runtime configuration.

## [1.0.40] - 2026-10-27

### Added

- Integration guidance provides generated, schema-backed responses for MCP
  clients.

### Changed

- Knowledge-tool discovery and its metadata were refreshed for AI clients.

## [1.0.39] - 2026-10-22

### Added

- Grafana investigation tools, Kubernetes pod-health context, Argo CD tools,
  incident capabilities, and ChatGPT widgets.
- Workspace isolation for token-authenticated MCP requests.

### Fixed

- Platform Slack errors retain their original error codes.
- Grafana panel views support stat panels.

## [1.0.27] - 2026-07-21

### Added

- Grafana integration tools, Kubernetes connection health, and platform
  integration-status discovery.

### Changed

- MCP response schemas and tool metrics were expanded for client compatibility.

## [1.0.3] - 2026-05-27

### Added

- Initial MCP server, OAuth flow, Slack, Kubernetes, Docker, incident graph,
  alert, and external-status toolsets.
