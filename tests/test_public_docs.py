"""Offline conformance checks for the generated public documentation surface."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from incidentflow_mcp.http.routes.mcp_proxy import _MCP_METHODS, MCPASGIProxyRoute
from incidentflow_mcp.tools.registry import get_tool_specs

REPO_ROOT = Path(__file__).parents[1]
FERN_ROOT = REPO_ROOT / "fern"
OPENAPI_PATH = REPO_ROOT / "openapi" / "openapi.yaml"
DOCS_CONFIG_PATH = FERN_ROOT / "docs.yml"
MANIFEST_PATH = REPO_ROOT / "docs" / "docs-governance.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

RELEASE_BLOCKED_TOOLS = {
    "memory_search_similar_incidents",
    "memory_get_service_context",
    "memory_upsert_incident_summary",
    "memory_find_runbook",
}
CONDITIONAL_TOOLS = {
    "external_status_check",
    "slack_alerts_list",
    "slack_alert_thread_get",
    "incident_thread_summary",
    "k8s_connection_health",
    "k8s_cluster_overview",
    "k8s_namespace_overview",
    "k8s_rbac_check",
    "k8s_agent_status",
    "k8s_list_namespaces",
    "k8s_list_pods",
    "k8s_get_pod",
    "k8s_get_pod_logs",
    "k8s_list_events",
    "k8s_list_deployments",
    "k8s_list_services",
    "k8s_get_rollout_status",
    "k8s_show_unhealthy_pods",
    "k8s_analyze_workload",
    "k8s_describe_pod",
    "k8s_debug_pod",
    "grafana_list_dashboards",
    "grafana_get_dashboard",
    "grafana_extract_panel_queries",
    "grafana_metrics_query",
    "grafana_metrics_query_range",
    "analyze_dashboard_health",
    "grafana_get_panel_view",
    "argocd_connection_health",
    "argocd_list_applications",
    "argocd_get_application",
    "argocd_get_application_resources",
    "argocd_get_sync_history",
    "argocd_get_last_operation",
    "argocd_find_recent_deployments",
    "argocd_analyze_application",
    "private_knowledge_search",
    "knowledge_get",
    "knowledge_upsert",
}


def _configured_pages() -> list[Path]:
    config = yaml.safe_load(DOCS_CONFIG_PATH.read_text(encoding="utf-8"))
    pages: list[Path] = []
    for section in config["navigation"]:
        for item in section.get("contents", []):
            if "path" in item:
                pages.append(FERN_ROOT / item["path"])
    return pages


def _bash_blocks() -> list[tuple[Path, str]]:
    blocks: list[tuple[Path, str]] = []
    for page in _configured_pages():
        content = page.read_text(encoding="utf-8")
        blocks.extend(
            (page, block.strip())
            for block in re.findall(r"```bash\n(.*?)\n```", content, flags=re.DOTALL)
        )
    return blocks


def _sse_json(response_text: str) -> dict[str, Any]:
    data_line = next(
        line.removeprefix("data: ")
        for line in response_text.splitlines()
        if line.startswith("data: ")
    )
    payload = json.loads(data_line)
    assert isinstance(payload, dict)
    return payload


def _normalize_route_path(path: str) -> str:
    return re.sub(r"{([^}:]+):[^}]+}", r"{\1}", path)


def _resolve_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    assert ref.startswith("#/"), f"unsupported external JSON Schema reference: {ref}"
    resolved: Any = root_schema
    for part in ref[2:].split("/"):
        resolved = resolved[part]
    assert isinstance(resolved, dict)
    return resolved


def _assert_json_schema(instance: Any, schema: dict[str, Any], root_schema: dict[str, Any]) -> None:
    if "$ref" in schema:
        _assert_json_schema(instance, _resolve_ref(root_schema, schema["$ref"]), root_schema)
        return

    if "oneOf" in schema:
        matches = 0
        for variant in schema["oneOf"]:
            try:
                _assert_json_schema(instance, variant, root_schema)
            except (AssertionError, KeyError, TypeError):
                continue
            matches += 1
        assert matches == 1, f"expected exactly one matching schema, got {matches}: {instance!r}"
        return

    if "enum" in schema:
        assert instance in schema["enum"]

    schema_type = schema.get("type")
    if schema_type == "object":
        assert isinstance(instance, dict)
        required = set(schema.get("required", []))
        missing = required - set(instance)
        assert not missing, f"missing required fields: {sorted(missing)}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(instance) <= set(properties)
        for name, value in instance.items():
            if name in properties:
                _assert_json_schema(value, properties[name], root_schema)
    elif schema_type == "array":
        assert isinstance(instance, list)
        assert len(instance) >= schema.get("minItems", 0)
        if "maxItems" in schema:
            assert len(instance) <= schema["maxItems"]
        for item in instance:
            _assert_json_schema(item, schema.get("items", {}), root_schema)
    elif schema_type == "string":
        assert isinstance(instance, str)
    elif schema_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool)
        if "minimum" in schema:
            assert instance >= schema["minimum"]
        if "maximum" in schema:
            assert instance <= schema["maximum"]
    elif schema_type == "boolean":
        assert isinstance(instance, bool)
    elif schema_type == "null":
        assert instance is None


def test_fern_navigation_is_grouped_complete_and_local_links_resolve() -> None:
    config = yaml.safe_load(DOCS_CONFIG_PATH.read_text(encoding="utf-8"))
    section_names = [section["section"] for section in config["navigation"]]
    assert section_names == [
        "Getting Started",
        "Using the API",
        "MCP Tools",
        "HTTP API Reference",
        "Releases",
    ]

    pages = _configured_pages()
    assert len(pages) == len(set(pages))
    assert all(page.is_file() for page in pages)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_pages = {
        REPO_ROOT / document["path"]
        for document in manifest["documents"]
        if document["visibility"] == "public"
        and document["published"]
        and Path(document["path"]).suffix in {".md", ".mdx"}
    }
    assert set(pages) == manifest_pages

    for page in pages:
        content = page.read_text(encoding="utf-8")
        assert "Human documentation review: pending" in content
        metadata = next(
            document
            for document in manifest["documents"]
            if REPO_ROOT / document["path"] == page
        )
        assert f"Contract version: `{metadata['version']}`" in content
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", content):
            if target.startswith(("https://", "http://", "#")):
                continue
            target_path = (
                FERN_ROOT / target.removeprefix("/")
                if target.startswith("/")
                else page.parent / target
            )
            assert target_path.resolve().is_file(), f"broken link in {page}: {target}"


def test_catalog_indexes_every_canonical_tool_with_release_status() -> None:
    catalog = (FERN_ROOT / "pages/reference/tool-catalog.mdx").read_text(encoding="utf-8")
    registry_names = {spec.name for spec in get_tool_specs()}
    documented_names = Counter(re.findall(r"`([a-z][a-z0-9_]+)`", catalog))
    assert {name for name in documented_names if name in registry_names} == registry_names
    assert all(documented_names[name] == 1 for name in registry_names)

    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    schemas = openapi["components"]["schemas"]
    for spec in get_tool_specs():
        schema = schemas[f"{spec.name}Arguments"]
        if not spec.submission_ready:
            expected = "not-public"
        elif spec.name in RELEASE_BLOCKED_TOOLS:
            expected = "release-blocked"
        elif spec.name in CONDITIONAL_TOOLS:
            expected = "conditional"
        else:
            expected = "available"
        assert schema["x-incidentflow-availability"] == expected
        assert schema["x-incidentflow-title"] == spec.title
        assert schema["x-mcp-behavior"] == spec.annotations


@pytest.mark.xfail(
    reason=(
        "human-governed Fern catalog has not yet been regenerated for the approved D4/D6 and "
        "Kubernetes bookkeeping contracts"
    ),
    strict=False,
)
def test_fern_catalog_matches_approved_release_contract_language() -> None:
    catalog = (FERN_ROOT / "pages/reference/tool-catalog.mdx").read_text(
        encoding="utf-8"
    ).lower()

    assert "`incident_summary` | not public" in catalog
    assert "without an implicit" in catalog
    assert "command/result rows" in catalog
    assert "bookkeeping" in catalog


def test_generated_openapi_operations_match_the_runtime_routes(
    unauth_client: TestClient,
) -> None:
    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    documented_operations = {
        (path, method.upper())
        for path, path_item in openapi["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }

    runtime_operations: set[tuple[str, str]] = set()
    for route in unauth_client.app.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            runtime_operations.update(
                (_normalize_route_path(route.path), method) for method in route.methods
            )
        elif isinstance(route, MCPASGIProxyRoute):
            runtime_operations.update(
                (_normalize_route_path(route._path), method) for method in _MCP_METHODS
            )

    assert documented_operations == runtime_operations


def test_versioned_openapi_download_is_exact_and_public_only() -> None:
    canonical = OPENAPI_PATH.read_bytes()
    spec = yaml.safe_load(canonical)
    version = spec["info"]["version"]
    download = FERN_ROOT / f"assets/downloads/incidentflow-mcp-openapi-{version}.yaml"

    assert download.read_bytes() == canonical
    assert spec["info"]["x-incidentflow-release-status"] == "candidate"
    assert spec["info"]["x-incidentflow-human-review"] == "pending"
    assert spec["servers"] == [
        {
            "url": "https://mcp.incidentflow.io",
            "description": "IncidentFlow MCP production origin",
        }
    ]
    assert not any(path.startswith(("/internal", "/admin")) for path in spec["paths"])


def test_public_operations_disclose_auth_errors_and_review_status() -> None:
    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    operations: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete", "options"}:
                operations.append((path, method, operation))

    assert operations
    for path, method, operation in operations:
        assert operation.get("description"), f"{method.upper()} {path} lacks a description"
        assert operation.get("tags"), f"{method.upper()} {path} lacks a navigation group"
        assert operation.get("x-incidentflow-availability") == "candidate"
        assert operation.get("x-incidentflow-human-review") == "pending"
        assert "200" in operation.get("responses", {})
        assert "500" in operation["responses"]
        assert "security" in operation, f"{method.upper()} {path} has ambiguous auth"

    for method in ("get", "post", "options"):
        mcp = spec["paths"]["/mcp"][method]
        assert mcp["security"] == [{"bearerAuth": []}]
        assert {"401", "403", "429", "503"} <= set(mcp["responses"])

    assert spec["paths"]["/metrics"]["get"]["security"] == [{"bearerAuth": []}]


def test_public_examples_are_placeholder_only_and_match_openapi_schema() -> None:
    examples_page = (FERN_ROOT / "pages/guides/safe-examples.mdx").read_text(encoding="utf-8")
    assert "if_pat_" not in examples_page
    assert "xoxb-" not in examples_page
    assert "ghp_" not in examples_page

    payloads = [
        json.loads(raw)
        for raw in re.findall(r"--data '(.*?)'\n```", examples_page, flags=re.DOTALL)
    ]
    assert [payload["method"] for payload in payloads] == [
        "initialize",
        "tools/list",
        "tools/call",
    ]

    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    request_schema = openapi["components"]["schemas"]["JsonRpcRequest"]
    for payload in payloads:
        _assert_json_schema(payload, request_schema, openapi)


def test_every_machine_executable_public_example_runs_offline(
    auth_client: TestClient,
    valid_auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    blocks = _bash_blocks()
    export_blocks = [block for _, block in blocks if block.startswith("export ")]
    mcp_blocks = [block for _, block in blocks if block.startswith("curl --request POST")]
    installer_blocks = [
        block for _, block in blocks if block.startswith("curl --fail --show-error")
    ]
    verifier_blocks = [block for _, block in blocks if block.startswith("python -c ")]
    assert len(blocks) == 6
    assert [len(export_blocks), len(mcp_blocks), len(installer_blocks), len(verifier_blocks)] == [
        1,
        3,
        1,
        1,
    ]

    assert export_blocks[0].splitlines() == [
        'export INCIDENTFLOW_MCP_URL="https://mcp.incidentflow.io/mcp"',
        'export INCIDENTFLOW_ACCESS_TOKEN="<access-token>"',
    ]

    payloads = []
    for block in mcp_blocks:
        assert '"$INCIDENTFLOW_MCP_URL"' in block
        assert '"Authorization: Bearer $INCIDENTFLOW_ACCESS_TOKEN"' in block
        assert '"Content-Type: application/json"' in block
        assert '"Accept: application/json, text/event-stream"' in block
        raw_payload = re.search(r"--data '(.*?)'\Z", block, flags=re.DOTALL)
        assert raw_payload is not None
        payloads.append(json.loads(raw_payload.group(1)))

    with auth_client as client:
        responses = [
            client.post(
                "/mcp",
                headers={
                    **valid_auth_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json=payload,
            )
            for payload in payloads
        ]
        installer_response = client.get(
            "/install.sh",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mcp.incidentflow.io",
            },
        )

    assert all(response.status_code == 200 for response in responses)
    initialize, tools_list, correlate = (_sse_json(response.text) for response in responses)
    assert initialize["result"]["protocolVersion"] == "2024-11-05"
    runtime_names = {tool["name"] for tool in tools_list["result"]["tools"]}
    assert runtime_names == {spec.name for spec in get_tool_specs()}
    assert correlate["result"].get("isError") is not True
    correlation = json.loads(correlate["result"]["content"][0]["text"])
    assert correlation["total_alerts"] == 1
    assert correlation["clusters"][0]["alert_ids"] == ["juniper-qa-001"]

    installer_block = installer_blocks[0]
    assert installer_block.splitlines() == [
        "curl --fail --show-error --silent https://mcp.incidentflow.io/install.sh "
        "--output incidentflow-install.sh",
        "sed -n '1,160p' incidentflow-install.sh",
        "bash incidentflow-install.sh --dry-run",
    ]
    assert installer_response.status_code == 200
    installer_path = tmp_path / "incidentflow-install.sh"
    installer_path.write_text(installer_response.text, encoding="utf-8")
    inspected = subprocess.run(
        ["sed", "-n", "1,160p", installer_path.name],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert inspected.stdout.startswith("#!/usr/bin/env bash")
    config_path = tmp_path / "must-not-exist.json"
    env = {**os.environ, "INCIDENTFLOW_CONFIG_FILE": str(config_path)}
    dry_run = subprocess.run(
        ["bash", installer_path.name, "--dry-run"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Dry run completed." in dry_run.stdout
    assert not config_path.exists()

    verifier_tokens = shlex.split(verifier_blocks[0])
    assert verifier_tokens[:2] == ["python", "-c"]
    download = FERN_ROOT / "assets/downloads/incidentflow-mcp-openapi-0.1.0.yaml"
    verified = subprocess.run(
        [sys.executable, "-c", verifier_tokens[2]],
        cwd=download.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "'version': '0.1.0'" in verified.stdout


@pytest.mark.parametrize(
    "unsupported_claim",
    [
        "GitHub integration is available",
        "PagerDuty integration is available",
        "Jira integration is available",
        "Datadog integration is available",
        "CloudWatch integration is available",
    ],
)
def test_public_docs_do_not_reintroduce_unavailable_integration_claims(
    unsupported_claim: str,
) -> None:
    public_docs = "\n".join(page.read_text(encoding="utf-8") for page in _configured_pages())
    assert unsupported_claim not in public_docs
