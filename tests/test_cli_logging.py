import os

from incidentflow_mcp.cli.main import _propagate_server_settings_to_reload_child


def test_serve_logging_settings_are_propagated_to_reload_child(
    monkeypatch,
) -> None:
    for key in ("HOST", "PORT", "LOG_LEVEL", "LOG_FORMAT", "LIBRARY_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    _propagate_server_settings_to_reload_child(
        host="127.0.0.1",
        port=8001,
        log_level="debug",
        log_format="json",
        library_log_level="error",
    )

    assert os.environ["HOST"] == "127.0.0.1"
    assert os.environ["PORT"] == "8001"
    assert os.environ["LOG_LEVEL"] == "debug"
    assert os.environ["LOG_FORMAT"] == "json"
    assert os.environ["LIBRARY_LOG_LEVEL"] == "error"
