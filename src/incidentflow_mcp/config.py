"""
Application configuration loaded from environment variables.

Copy `.env.example` to `.env` and adjust values for local development.
All settings can be overridden via real environment variables without a file.
"""

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    # -----------------------------------------------------------------------
    # Server
    # -----------------------------------------------------------------------
    host: str = Field(default="0.0.0.0", description="Bind host")
    port: int = Field(default=8000, description="Bind port")
    log_level: str = Field(default="info", description="Logging level")
    environment: str = Field(default="development", description="Environment name")
    allow_unprotected_in_production: bool = Field(
        default=False,
        description=(
            "Allow startup in production without auth providers. "
            "Default false (fail-closed)."
        ),
    )

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------
    # Set this to a valid Bearer PAT to protect the MCP endpoint.
    # If empty, the server starts in UNPROTECTED mode with a warning.
    incidentflow_pat: SecretStr | None = Field(
        default=None,
        description="Static Bearer PAT for local dev auth (INCIDENTFLOW_PAT)",
    )

    # Optional managed-token verification via platform-api.
    # When set, Bearer tokens are introspected remotely via
    # POST {PLATFORM_API_BASE_URL}/api/v1/tokens/introspect.
    platform_api_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_API_BASE_URL", "INCIDENTFLOW_API_BASE_URL"),
        description="Base URL for managed token introspection (e.g. http://127.0.0.1:8000)",
    )
    platform_api_introspect_path: str = Field(
        default="/api/v1/tokens/introspect",
        description="Path to managed token introspection endpoint on platform-api",
    )
    platform_api_timeout_seconds: float = Field(
        default=5.0,
        description="HTTP timeout for token introspection calls",
    )
    oauth_expected_issuer: str | None = Field(
        default=None,
        description="Expected issuer for OAuth JWT access tokens",
    )
    oauth_jwks_url: str | None = Field(
        default=None,
        description="JWKS URL for OAuth access token signature verification",
    )
    auth_mode: str = Field(
        default="dual",
        description="Auth mode. dual keeps OAuth + PAT fallback paths enabled.",
    )
    platform_api_internal_api_key: SecretStr | None = Field(
        default=None,
        description="Optional service-to-service API key for MCP -> platform-api calls",
    )
    platform_api_ai_jobs_path: str = Field(
        default="/api/v1/ai/jobs",
        description="Path for MCP async job submit/poll endpoints on platform-api",
    )
    platform_api_ai_poll_after_seconds: int = Field(
        default=2,
        ge=1,
        le=60,
        description="Suggested poll interval in async MCP tool responses",
    )
    mcp_default_workspace_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MCP_DEFAULT_WORKSPACE_ID", "DEFAULT_WORKSPACE_ID"),
        description="Optional default workspace_id used for async job orchestration when tool input omits workspace_id.",
    )

    # When True, tokens must carry all required scopes for the endpoint they
    # access; requests missing a scope get 403.  Defaults to True in
    # production, False in all other environments (dev-friendly mode).
    enforce_scopes: bool | None = Field(
        default=None,
        description=(
            "Enforce token scopes on every request. "
            "Defaults to True when ENVIRONMENT=production, False otherwise. "
            "Override explicitly with ENFORCE_SCOPES=true|false."
        ),
    )

    def scopes_enforced(self) -> bool:
        """Return the effective scope-enforcement flag."""
        if self.enforce_scopes is not None:
            return self.enforce_scopes
        return self.environment == "production"

    def managed_token_introspection_enabled(self) -> bool:
        """Return True when remote managed-token introspection is configured."""
        return bool(self.platform_api_base_url)

    def async_tools_enabled(self) -> bool:
        if self.mcp_async_tools_enabled is not None:
            return self.mcp_async_tools_enabled
        return self.environment == "production"

    # -----------------------------------------------------------------------
    # MCP
    # -----------------------------------------------------------------------
    mcp_server_name: str = Field(default="incidentflow-mcp", description="MCP server name")
    mcp_server_version: str = Field(default="0.1.0", description="MCP server version")
    mcp_canonical_resource: str = Field(
        default="https://mcp.incidentflow.io/mcp",
        description="Canonical OAuth resource identifier for this MCP server",
    )
    mcp_resource_metadata_url: str = Field(
        default="https://mcp.incidentflow.io/.well-known/oauth-protected-resource",
        description="Canonical OAuth protected resource metadata URL",
    )
    mcp_session_idle_timeout_seconds: int = Field(
        default=1800,
        description=(
            "Idle timeout for inferred MCP sessions; expired sessions are "
            "marked as terminated for metrics."
        ),
    )
    mcp_async_tools_enabled: bool | None = Field(
        default=None,
        description="Enable async orchestration for heavy MCP tools (default: True in production).",
    )
    mcp_oms_persist_enabled: bool = Field(
        default=False,
        description="When true, external status tool requests trigger OMS persistence side-effects.",
    )

    # -----------------------------------------------------------------------
    # Example tool knobs (extend as needed)
    # -----------------------------------------------------------------------
    max_alert_correlation_window_minutes: int = Field(
        default=60,
        description="Time window used by correlate_alerts tool",
    )

    # -----------------------------------------------------------------------
    # Rate limiting / Redis
    # -----------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used for distributed rate limiting state",
    )
    rate_limit_unauth_per_min: int = Field(
        default=20,
        description="Transport-level requests per minute for unauthenticated IPs",
    )
    rate_limit_authenticated_per_min: int = Field(
        default=60,
        validation_alias=AliasChoices("RATE_LIMIT_AUTHENTICATED_PER_MIN", "RATE_LIMIT_FREE_PER_MIN"),
        description="Transport-level requests per minute for authenticated identities",
    )
    tool_limit_authenticated_per_min: int = Field(
        default=20,
        validation_alias=AliasChoices("TOOL_LIMIT_AUTHENTICATED_PER_MIN", "TOOL_LIMIT_FREE_PER_MIN"),
        description="MCP tools/call requests per minute for authenticated identities",
    )
    expensive_tool_limit_per_min: int = Field(
        default=5,
        description="Per-minute limit for expensive tools (per identity)",
    )
    tool_concurrency_authenticated: int = Field(
        default=2,
        validation_alias=AliasChoices("TOOL_CONCURRENCY_AUTHENTICATED", "TOOL_CONCURRENCY_FREE"),
        description="Max concurrent tool executions for authenticated identities",
    )
    tool_timeout_seconds: int = Field(
        default=30,
        description="Default timeout (seconds) for tool execution",
    )
    expensive_tools: str = Field(
        default="incident_graph_build,large_correlation,slack_thread_mining,github_org_search",
        description="Comma-separated list of expensive MCP tools",
    )
    tool_timeout_overrides: str = Field(
        default="",
        description="Comma-separated per-tool timeout overrides, e.g. 'tool_a=15,tool_b=45'",
    )
    rate_limit_auth_endpoints: str = Field(
        default="/authorize,/token,/register,/oauth/register",
        description="Comma-separated auth endpoint prefixes to protect with transport-level rate limiting",
    )
    rate_limit_authenticated_bucket_scope: str = Field(
        default="principal",
        description="Bucket scope for authenticated identities: ip | principal | workspace",
    )
    rate_limit_unauthenticated_bucket_scope: str = Field(
        default="ip",
        description="Bucket scope for unauthenticated identities: ip | principal | workspace",
    )
    metrics_trusted_cidrs: str = Field(
        default="127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        description=(
            "Comma-separated CIDRs allowed to read /metrics without bearer auth. "
            "Use private cluster CIDRs to keep Prometheus scraping working."
        ),
    )

    def expensive_tools_set(self) -> set[str]:
        return {item.strip() for item in self.expensive_tools.split(",") if item.strip()}

    def tool_timeout_overrides_map(self) -> dict[str, int]:
        overrides: dict[str, int] = {}
        for raw in self.tool_timeout_overrides.split(","):
            part = raw.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            tool = name.strip()
            if not tool:
                continue
            try:
                seconds = int(value.strip())
            except ValueError:
                continue
            if seconds > 0:
                overrides[tool] = seconds
        return overrides

    def rate_limited_auth_endpoints(self) -> list[str]:
        return [item.strip() for item in self.rate_limit_auth_endpoints.split(",") if item.strip()]

    def metrics_trusted_cidrs_list(self) -> list[str]:
        return [item.strip() for item in self.metrics_trusted_cidrs.split(",") if item.strip()]

    def oauth_validation_enabled(self) -> bool:
        return bool(self.oauth_expected_issuer and self.oauth_jwks_url)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
