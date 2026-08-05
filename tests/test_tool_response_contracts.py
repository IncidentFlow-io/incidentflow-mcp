"""Tests for global MCP tool response contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.tools.contracts import (
    RESERVED_CONTRACT_KEYS,
    TOOL_SCHEMA_VERSION,
    apply_tool_contract,
    export_tool_schemas,
    schema_id_for_tool,
    schema_url,
    tool_response_model,
    tool_response_models,
)
from incidentflow_mcp.tools.registry import get_tool_specs


def test_every_registered_tool_has_response_model() -> None:
    specs = get_tool_specs()
    models = tool_response_models(specs)

    assert set(models) == {spec.name for spec in specs}
    for spec in specs:
        if spec.name == "argocd_get_last_operation":
            continue
        schema_id = schema_id_for_tool(spec.name)
        model = models[spec.name]
        payload = model.model_validate(
            {
                "schemaVersion": TOOL_SCHEMA_VERSION,
                "schemaId": schema_id,
                "ok": True,
                "warnings": [],
                "tool_specific_field": "allowed in v1",
            }
        )

        assert payload.schemaVersion == TOOL_SCHEMA_VERSION
        assert payload.schemaId == schema_id
        assert payload.model_extra == {"tool_specific_field": "allowed in v1"}


def test_schema_id_conventions_are_stable() -> None:
    assert schema_id_for_tool("argocd_get_last_operation") == "argocd.get-last-operation"
    assert schema_id_for_tool("grafana_metrics_query") == "grafana.metrics-query"
    assert schema_id_for_tool("k8s_list_pods") == "kubernetes.list-pods"
    assert schema_id_for_tool("slack_alerts_list") == "slack.alerts-list"
    assert schema_id_for_tool("knowledge_upsert") == "knowledge.upsert"
    assert schema_id_for_tool("mcp_version") == "platform.mcp-version"


def test_apply_tool_contract_is_additive() -> None:
    payload = {"ok": True, "total": 3}

    stamped = apply_tool_contract(payload, tool_name="k8s_list_pods")

    assert stamped == {
        "ok": True,
        "total": 3,
        "schemaVersion": "v1",
        "schemaId": "kubernetes.list-pods",
        "warnings": [],
    }
    assert payload == {"ok": True, "total": 3}


def test_apply_tool_contract_does_not_overwrite_reserved_keys() -> None:
    payload = {
        "schemaVersion": "custom",
        "schemaId": "custom.schema",
        "warnings": ["already present"],
    }

    stamped = apply_tool_contract(payload, tool_name="k8s_list_pods")

    assert stamped["schemaVersion"] == "custom"
    assert stamped["schemaId"] == "custom.schema"
    assert stamped["warnings"] == ["already present"]


def test_export_tool_schemas_writes_envelope_and_one_file_per_tool(tmp_path: Path) -> None:
    specs = get_tool_specs()

    written = export_tool_schemas(specs, tmp_path)

    assert len(written) == len(specs) + 1
    assert tmp_path.joinpath("tool-envelope.v1.schema.json").exists()
    assert tmp_path.joinpath("argocd.get-last-operation.v1.schema.json").exists()
    assert tmp_path.joinpath("kubernetes.list-pods.v1.schema.json").exists()


def test_export_tool_schemas_includes_canonical_ids(tmp_path: Path) -> None:
    export_tool_schemas(get_tool_specs(), tmp_path)

    envelope = tmp_path.joinpath("tool-envelope.v1.schema.json").read_text()
    argocd = tmp_path.joinpath("argocd.get-last-operation.v1.schema.json").read_text()

    assert schema_url("tool-envelope.v1.schema.json") in envelope
    assert schema_url("argocd.get-last-operation.v1.schema.json") in argocd


def test_reserved_contract_keys_are_documented() -> None:
    assert RESERVED_CONTRACT_KEYS == {"schemaVersion", "schemaId", "warnings"}


def test_argocd_last_operation_requires_application_name() -> None:
    model = tool_response_model("argocd_get_last_operation")

    valid = model.model_validate(
        {
            "schemaVersion": "v1",
            "schemaId": "argocd.get-last-operation",
            "ok": True,
            "warnings": [],
            "application_name": "incidentflow-mcp-dev",
            "status": "ok",
            "operation": {
                "phase": "Succeeded",
                "resource_results": [
                    {
                        "kind": "Deployment",
                        "namespace": "incidentflow-dev",
                        "name": "incidentflow-mcp",
                        "status": "Synced",
                    }
                ],
            },
        }
    )

    assert valid.application_name == "incidentflow-mcp-dev"


def test_argocd_last_operation_rejects_app_name_rename() -> None:
    model = tool_response_model("argocd_get_last_operation")

    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(
            {
                "schemaVersion": "v1",
                "schemaId": "argocd.get-last-operation",
                "ok": True,
                "warnings": [],
                "app_name": "incidentflow-mcp-dev",
                "status": "ok",
                "operation": None,
            }
        )

    errors = exc_info.value.errors()
    assert any(error["loc"] == ("application_name",) for error in errors)
    assert any(
        error["loc"] == ("app_name",) and error["type"] == "extra_forbidden" for error in errors
    )


@pytest.mark.asyncio
async def test_real_meta_tool_outputs_get_reserved_keys_from_contract_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            environment="development",
            redis_url="redis://test-only",
        ),
    )
    mcp = create_mcp_server()

    for tool_name in ("mcp_version", "incidentflow_capabilities"):
        result = await mcp._tool_manager.call_tool(tool_name, {})
        for key in RESERVED_CONTRACT_KEYS:
            assert key in result
        assert result["schemaVersion"] == TOOL_SCHEMA_VERSION
        assert result["schemaId"] == schema_id_for_tool(tool_name)


@pytest.mark.asyncio
async def test_real_meta_tool_outputs_validate_against_response_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(
            _env_file=None,
            incidentflow_pat="test-secret-token",
            environment="development",
            redis_url="redis://test-only",
        ),
    )
    mcp = create_mcp_server()

    for tool_name in ("mcp_version", "incidentflow_capabilities"):
        result = await mcp._tool_manager.call_tool(tool_name, {})
        model = tool_response_model(tool_name)
        payload = model.model_validate(result)

        assert payload.schemaVersion == TOOL_SCHEMA_VERSION
        assert payload.schemaId == schema_id_for_tool(tool_name)


@pytest.mark.asyncio
async def test_structured_validation_errors_are_stamped_with_tool_schema() -> None:
    mcp = create_mcp_server()

    result = await mcp._tool_manager.call_tool(
        "k8s_get_pod",
        {"namespace": "default", "pod": "api-123", "tail_lines_typo": 10},
    )
    payload = tool_response_model("k8s_get_pod").model_validate(result)

    assert payload.schemaVersion == TOOL_SCHEMA_VERSION
    assert payload.schemaId == "kubernetes.get-pod"
    assert payload.ok is False
    assert payload.status == "failed"
    assert payload.error is not None
