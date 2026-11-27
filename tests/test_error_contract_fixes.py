"""Regression tests for error-contract findings from the live dev tool sweep."""

from __future__ import annotations

import httpx

from incidentflow_mcp.mcp.compatibility.fastmcp_contracts import (
    _nested_argocd_failure,
    _payload_warnings,
)
from incidentflow_mcp.mcp.errors import map_exception
from incidentflow_mcp.tools.contracts import ErrorCode

_INTERNAL_URL = "http://incidentflow-platform-api.incidentflow-dev.svc.cluster.local/api/v1/x"


def test_upstream_http_error_message_does_not_leak_internal_url() -> None:
    request = httpx.Request("GET", _INTERNAL_URL)
    response = httpx.Response(
        422,
        request=request,
        json={"code": "invalid_incident_id", "message": "Malformed incident_id"},
    )
    fields = map_exception(httpx.HTTPStatusError("boom", request=request, response=response))

    assert fields["code"] == ErrorCode.INVALID_ARGUMENT
    assert "svc.cluster.local" not in fields["message"]
    assert "http://" not in fields["message"]
    assert fields["message"] == "Upstream request failed with HTTP 422: Malformed incident_id"
    assert fields["details"]["http_status"] == 422


def test_argocd_ok_false_payload_becomes_error_with_not_found_hint() -> None:
    payload = {
        "source": {"type": "argocd"},
        "ok": False,
        "status": "permission_denied",
        "error": {
            "code": "ARGOCD_RBAC_DENIED",
            "message": "Argo CD token is missing required RBAC permission",
            "http_status": 403,
            "details": {"application_name": "does-not-exist", "argocd_http_status": 403},
        },
    }
    failure = _nested_argocd_failure(payload)

    assert failure is not None
    assert failure["code"] == ErrorCode.PERMISSION_DENIED
    assert "not found or is not visible" in failure["message"]
    assert "argocd_list_applications" in failure["message"]
    assert failure["details"]["argocd_error"]["code"] == "ARGOCD_RBAC_DENIED"


def test_argocd_success_and_non_dict_payloads_are_left_alone() -> None:
    assert _nested_argocd_failure({"ok": True, "applications": []}) is None
    assert _nested_argocd_failure({"applications": []}) is None
    assert _nested_argocd_failure({"ok": False, "error": "text"}) is None
    assert _nested_argocd_failure(["x"]) is None
    unavailable = _nested_argocd_failure(
        {"ok": False, "error": {"message": "Argo CD unreachable", "http_status": 503}}
    )
    assert unavailable is not None
    assert unavailable["code"] == ErrorCode.UPSTREAM_ERROR


def test_payload_warnings_are_lifted_into_meta() -> None:
    assert _payload_warnings({"warnings": ["Dashboard panels trimmed to 2.", 3]}) == [
        "Dashboard panels trimmed to 2."
    ]
    assert _payload_warnings({"warnings": None}) == []
    assert _payload_warnings([1, 2]) == []


async def test_logs_client_keeps_only_short_policy_reason() -> None:
    import pytest

    from incidentflow_mcp.config import Settings
    from incidentflow_mcp.platform_api.logs_client import PlatformLogsClient, PlatformLogsError

    settings = Settings(
        _env_file=None,
        platform_api_base_url="https://platform.invalid",
        platform_api_internal_api_key="internal-key",
        redis_url="redis://test-only",
    )
    source = "f12f7829-0ed4-463c-8bd6-9f6f459bcc28"

    def client_for(status: int, body: object) -> PlatformLogsClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
        return PlatformLogsClient(settings, "bearer", transport=transport)

    with pytest.raises(PlatformLogsError) as caught:
        await client_for(
            400, {"code": "invalid_request", "message": "Log time range exceeds source lookback"}
        ).query({"source_id": source})
    assert caught.value.message == ("Invalid Logs request: Log time range exceeds source lookback")

    with pytest.raises(PlatformLogsError) as caught:
        await client_for(500, {"message": "db exploded"}).query({"source_id": source})
    assert caught.value.message == "Logs service unavailable"

    with pytest.raises(PlatformLogsError) as caught:
        await client_for(400, {"message": "see http://internal.svc/x?token=abc"}).query(
            {"source_id": source}
        )
    assert caught.value.message == "Invalid Logs request"

    with pytest.raises(PlatformLogsError) as caught:
        await client_for(200, {}).query({"source_id": "not-a-uuid"})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert "logs_list_sources" in caught.value.message


async def test_integration_status_is_cached_briefly_per_workspace(monkeypatch) -> None:
    from types import SimpleNamespace

    from incidentflow_mcp import integrations
    from incidentflow_mcp.config import Settings

    settings = Settings(
        _env_file=None,
        platform_api_base_url="https://platform.invalid",
        platform_api_internal_api_key="internal-key",
        redis_url="redis://test-only",
    )
    calls: list[str] = []

    class FakeStatusClient:
        def __init__(self, _settings: object) -> None:
            pass

        async def get_workspace_status(self, *, workspace_id: str) -> dict:
            calls.append(workspace_id)
            return {}

    monkeypatch.setattr(integrations, "PlatformIntegrationStatusClient", FakeStatusClient)
    service = integrations.IntegrationStatusService(settings)
    principal = SimpleNamespace(workspace=SimpleNamespace(id="ws-1"))
    other = SimpleNamespace(workspace=SimpleNamespace(id="ws-2"))

    first = await service._internal_workspace_statuses(principal)  # type: ignore[arg-type]
    second = await service._internal_workspace_statuses(principal)  # type: ignore[arg-type]
    await service._internal_workspace_statuses(other)  # type: ignore[arg-type]
    assert first == second
    assert calls == ["ws-1", "ws-2"]

    monkeypatch.setattr(integrations, "_STATUS_CACHE_TTL_SECONDS", -1.0)
    integrations.IntegrationStatusService.clear_cache()
    await service._internal_workspace_statuses(principal)  # type: ignore[arg-type]
    await service._internal_workspace_statuses(principal)  # type: ignore[arg-type]
    assert calls == ["ws-1", "ws-2", "ws-1", "ws-1"]


async def test_read_only_tool_guard_uses_cached_entitlements(monkeypatch) -> None:
    from incidentflow_mcp.mcp import context as context_module

    seen: list[bool] = []

    class FakeEntitlements:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, workspace_id: str, *, allow_cached: bool = False):
            seen.append(allow_cached)
            raise context_module.EntitlementsUnavailable("down")

    monkeypatch.setattr(context_module, "PlatformEntitlementsClient", FakeEntitlements)
    monkeypatch.setattr(
        context_module, "get_current_auth_context", lambda: {"workspace_id": "ws-1"}
    )
    read_spec = type("S", (), {"annotations": {"readOnlyHint": True}})()
    write_spec = type("S", (), {"annotations": {"readOnlyHint": False}})()
    ctx = context_module.ToolRegistrationContext(
        mcp=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        specs={"read_tool": read_spec, "write_tool": write_spec},
    )
    assert "ENTITLEMENTS_UNAVAILABLE" in await ctx.resolve_tool_guard("read_tool")
    assert "ENTITLEMENTS_UNAVAILABLE" in await ctx.resolve_tool_guard("write_tool")
    assert seen == [True, False]
