"""Every registered tool's response validates against its published schema.

Hermetic: the platform-api base URL points at an unused local port so integration
tools fail fast and deterministically (connection refused → INTEGRATION_UNAVAILABLE).
Both success and error envelopes must validate against the tool's own schema.
"""

from __future__ import annotations

import logging

import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.contracts import build_output_schema

_ENVELOPE_KEYS = {
    "api_version",
    "schema_version",
    "schema_id",
    "status",
    "request_id",
    "data",
    "error",
    "meta",
}

# Minimal args so more tools reach a real code path instead of a bare arg error.
_ARGS: dict[str, dict] = {
    "incident_summary": {"incident_id": "inc_check"},
    "knowledge_upsert": {"document_type": "runbook", "title": "t", "text": "x", "dry_run": True},
    "knowledge_get": {"id": "doc_check"},
    "public_knowledge_search": {"query": "api", "limit": 2},
    "private_knowledge_search": {"query": "api", "limit": 2},
    "k8s_get_pod": {"namespace": "default", "pod": "p"},
    "k8s_describe_pod": {"namespace": "default", "pod": "p"},
    "k8s_debug_pod": {"namespace": "default", "pod": "p"},
    "k8s_get_pod_logs": {"namespace": "default", "pod": "p"},
    "k8s_namespace_overview": {"namespace": "default"},
    "k8s_list_pods": {"namespace": "default"},
    "k8s_analyze_workload": {"namespace": "default", "workload": "w"},
    "grafana_get_dashboard": {"dashboard_uid": "uid"},
    "grafana_extract_panel_queries": {"dashboard_uid": "uid"},
    "argocd_get_application": {"application_name": "app"},
    "external_status_check": {"providers": ["github"], "wait_for_result": False},
    "correlate_alerts": {
        "alerts": [
            {
                "alert_id": "a1",
                "name": "Down",
                "service": "svc",
                "severity": "critical",
                "status": "firing",
                "fired_at": "2026-08-07T09:00:00Z",
            }
        ]
    },
}


def _auth_context() -> dict:
    return {
        "authenticated": True,
        "auth_method": "oauth",
        "bearer_token": "check-token",
        "client_id": "check",
        "workspace_id": "ws_check",
        "workspace_name": "Check",
        "workspace_slug": "check",
        "workspace_role": "owner",
        "user_id": "u",
        "email": "c@example.com",
        "plan": None,
    }


@pytest.mark.asyncio
async def test_every_tool_response_validates_against_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            environment="development",
            redis_url="redis://test-only",
            mcp_default_workspace_id="ws_check",
            # Unused local port → deterministic fast connection failure.
            platform_api_base_url="http://127.0.0.1:9",
        ),
    )
    mcp = create_mcp_server()
    tools = sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name)
    assert len(tools) >= 40  # sanity: the full registry is loaded

    set_current_auth_context(_auth_context())
    failures: list[str] = []
    # This test drives every tool into a deterministic connection failure on
    # purpose; we assert on the returned error ENVELOPE, not on logs. Mute the
    # expected ConnectError WARNING tracebacks (log_cli is on) so the CI log
    # stays readable. Restored in `finally`.
    logging.disable(logging.ERROR)
    try:
        for tool in tools:
            name = tool.name
            validator = Draft202012Validator(
                build_output_schema(name), format_checker=FormatChecker()
            )
            result = await mcp._tool_manager.call_tool(name, _ARGS.get(name, {}))
            envelope = result.structuredContent if hasattr(result, "structuredContent") else result
            if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS:
                failures.append(f"{name}: not a canonical 8-key envelope")
                continue
            errors = sorted(validator.iter_errors(envelope), key=str)
            if errors:
                failures.append(
                    f"{name}: {envelope.get('status')} fails schema: {errors[0].message}"
                )
    finally:
        logging.disable(logging.NOTSET)
        clear_current_auth_context()

    assert not failures, "Schema conformance failures:\n" + "\n".join(failures)


# --- strict success shapes locked with representative samples ---------------
# Hermetic mode can only reach the error branch for integration/platform tools,
# so lock each strict tool's *success* payload with a representative sample.
_STRICT_SUCCESS_SAMPLES: dict[str, dict] = {
    "mcp_version": {
        "service": "incidentflow-mcp",
        "service_version": "1.0.0",
        "current_api_version": "v1",
        "contract_version": "1.0",
        "supported_api_versions": ["v1"],
        "supported_schema_versions": ["1.0"],
        "deprecated_api_versions": [],
        "environment": "dev",
        "tools": {"registered": 48, "operational": 44, "meta": 4},
        "image": {"signed": False, "signature_verified": False},
    },
    "k8s_agent_status": {
        "status": "connected",
        "healthy": True,
        "checked_at": "2026-08-07T09:29:30Z",
        "cluster_id": "c1",
    },
    "k8s_rbac_check": {
        "read_only": True,
        "checked_at": "2026-08-07T09:29:30Z",
        "permissions": {"list_pods": {"allowed": True}},
        "cluster_id": "c1",
    },
    "incident_thread_summary": {
        "title": "DB latency",
        "status": "investigating",
        "summary": "elevated p99",
        "what_engineers_said": ["looks like a bad deploy"],
        "probable_root_cause": None,
        "actions_taken": [],
        "next_actions": ["roll back"],
        "runbooks": [],
        "commands": [],
        "risks": [],
        "open_questions": [],
    },
    "knowledge_upsert_stored": {
        "stored": True,
        "type": "runbook",
        "id": "rb-1",
        "title": "t",
        "operation": "created",
        "created": True,
        "updated": False,
        "point_id": "p1",
        "text_hash": "abc",
    },
    "knowledge_upsert_dry_run": {
        "stored": False,
        "dry_run": True,
        "validated": True,
        "type": "runbook",
        "id": "rb-1",
        "point_id": None,
        "would_write": {"workspace_id": "ws", "type": "runbook", "title": "t"},
    },
}


@pytest.mark.parametrize("key", sorted(_STRICT_SUCCESS_SAMPLES))
def test_strict_tool_success_samples_validate(key: str) -> None:
    from incidentflow_mcp.tools.contracts import success_envelope

    tool_name = key.split("_dry_run")[0].split("_stored")[0] if "knowledge_upsert" in key else key
    envelope = success_envelope(_STRICT_SUCCESS_SAMPLES[key], tool_name=tool_name)
    jsonschema.Draft202012Validator(
        build_output_schema(tool_name), format_checker=FormatChecker()
    ).validate(envelope)
