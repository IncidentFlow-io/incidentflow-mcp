"""
Tests for the dynamic installer script endpoint.
"""

from fastapi.testclient import TestClient


class TestInstallScriptEndpoint:
    def test_install_script_is_public(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/install.sh")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert "inline; filename=\"install.sh\"" in resp.headers["content-disposition"]

    def test_install_script_uses_forwarded_headers(
        self, auth_client: TestClient
    ) -> None:
        resp = auth_client.get(
            "/install.sh",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "incidentflow.io",
            },
        )
        assert resp.status_code == 200
        assert resp.text.startswith("#!/usr/bin/env bash")
        assert "curl -fsSL https://incidentflow.io/install.sh | bash" in resp.text
        assert "curl -fsSL https://incidentflow.io/install.sh | bash -s -- --dry-run" in resp.text
        assert 'SERVER_URL="${INCIDENTFLOW_SERVER_URL:-https://incidentflow.io/mcp}"' in resp.text
        assert "warn \"Dry run mode: no files will be modified.\"" in resp.text
        assert "ok \"Dry run completed.\"" in resp.text
