"""Regression tests that keep CI deterministic across developer machines."""

from incidentflow_mcp.auth.repository import InMemoryTokenRepository, get_token_repository
from incidentflow_mcp.config import Settings


def test_settings_can_ignore_env_file_and_env_vars(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("PLATFORM_API_BASE_URL", "http://should-not-be-used:9999")
    settings = Settings(
        _env_file=None,
        incidentflow_pat="test",
        platform_api_base_url=None,
        environment="test",
        log_level="warning",
    )
    assert settings.platform_api_base_url is None


def test_token_repository_isolation_fixture_is_active() -> None:
    assert isinstance(get_token_repository(), InMemoryTokenRepository)
