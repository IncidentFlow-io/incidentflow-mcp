"""
Application configuration loaded from environment variables.

Copy `.env.example` to `.env` and adjust values for local development.
All settings can be overridden via real environment variables without a file.
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
