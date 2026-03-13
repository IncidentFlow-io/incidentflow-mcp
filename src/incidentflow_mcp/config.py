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

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------
    # Set this to a valid Bearer PAT to protect the MCP endpoint.
    # If empty, the server starts in UNPROTECTED mode with a warning.
    incidentflow_pat: SecretStr | None = Field(
        default=None,
        description="Static Bearer PAT for local dev auth (INCIDENTFLOW_PAT)",
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

    # -----------------------------------------------------------------------
    # MCP
    # -----------------------------------------------------------------------
    mcp_server_name: str = Field(default="incidentflow-mcp", description="MCP server name")
    mcp_server_version: str = Field(default="0.1.0", description="MCP server version")

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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
