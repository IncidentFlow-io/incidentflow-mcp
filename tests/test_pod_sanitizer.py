"""Tests for _sanitize_pod and _strip_image_digest.

Asserts that k8s_list_pods / k8s_get_pod never leak raw labels, image digests,
node internals, annotations, or other fields not on the explicit allowlist.
"""

from __future__ import annotations

import json

from incidentflow_mcp.mcp.services.kubernetes_analysis import _sanitize_pod, _strip_image_digest

# ---------------------------------------------------------------------------
# _strip_image_digest
# ---------------------------------------------------------------------------


def test_strip_image_digest_removes_sha256() -> None:
    image = "ghcr.io/myorg/myapp:v1.2.3@sha256:abc123def456"
    assert _strip_image_digest(image) == "ghcr.io/myorg/myapp:v1.2.3"


def test_strip_image_digest_leaves_plain_tag_unchanged() -> None:
    image = "ghcr.io/myorg/myapp:dev-v1.0.21"
    assert _strip_image_digest(image) == image


def test_strip_image_digest_leaves_digest_only_ref_up_to_at() -> None:
    image = "registry.example.com/app@sha256:deadbeef"
    assert _strip_image_digest(image) == "registry.example.com/app"


def test_strip_image_digest_empty_string() -> None:
    assert _strip_image_digest("") == ""


# ---------------------------------------------------------------------------
# _sanitize_pod — allowlisted fields only
# ---------------------------------------------------------------------------

_RAW_POD: dict = {
    "name": "incidentflow-mcp-76f5987dc5-j5r6d",
    "namespace": "incidentflow-dev",
    "labels": {
        "app": "incidentflow-mcp",
        "pod-template-hash": "76f5987dc5",
        "team": "platform",
    },
    "phase": "Running",
    "node_name": "incidentflow-worker-1",
    "containers": [
        {
            "name": "incidentflow-mcp",
            "ready": True,
            "restart_count": 0,
            "image": "ghcr.io/incidentflow-io/incidentflow-mcp:dev-v1.0.21@sha256:abc123",
        }
    ],
    "age": "8m31s",
    # Fields that must NEVER appear in output:
    "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "secret"},
    "owner_references": [{"kind": "ReplicaSet", "name": "incidentflow-mcp-76f5987dc5"}],
    "service_account_name": "incidentflow-mcp",
    "volumes": [{"name": "kube-api-access", "secret": {"secretName": "default-token"}}],
    "env": [{"name": "DB_PASSWORD", "value": "supersecret"}],
}


def test_sanitize_pod_default_fields_present() -> None:
    out = _sanitize_pod(_RAW_POD)
    assert out["name"] == "incidentflow-mcp-76f5987dc5-j5r6d"
    assert out["namespace"] == "incidentflow-dev"
    assert out["phase"] == "Running"
    assert out["ready"] is True
    assert out["restarts"] == 0
    assert out["age"] == "8m31s"
    assert out["node"] == "incidentflow-worker-1"
    assert len(out["containers"]) == 1


def test_sanitize_pod_strips_image_digest_by_default() -> None:
    out = _sanitize_pod(_RAW_POD)
    assert out["containers"][0]["image"] == "ghcr.io/incidentflow-io/incidentflow-mcp:dev-v1.0.21"


def test_sanitize_pod_excludes_labels_by_default() -> None:
    out = _sanitize_pod(_RAW_POD)
    assert "labels" not in out


def test_sanitize_pod_includes_labels_when_requested() -> None:
    out = _sanitize_pod(_RAW_POD, include_labels=True)
    assert "labels" in out
    assert out["labels"]["app"] == "incidentflow-mcp"


def test_sanitize_pod_excludes_node_when_disabled() -> None:
    out = _sanitize_pod(_RAW_POD, include_node=False)
    assert "node" not in out


def test_sanitize_pod_excludes_images_when_disabled() -> None:
    out = _sanitize_pod(_RAW_POD, include_images=False)
    for c in out["containers"]:
        assert "image" not in c


def test_sanitize_pod_never_exposes_blocked_fields() -> None:
    out = _sanitize_pod(_RAW_POD, include_labels=True, include_images=True, include_node=True)
    blocked = {
        "annotations",
        "owner_references",
        "ownerReferences",
        "service_account_name",
        "serviceAccountName",
        "volumes",
        "env",
        "envFrom",
        "managed_fields",
        "managedFields",
        "uid",
        "resourceVersion",
    }
    for field in blocked:
        assert field not in out, f"blocked field '{field}' leaked into sanitized pod"

    # Also check containers
    for c in out["containers"]:
        for field in ("imageID", "containerID", "image_id", "container_id"):
            assert field not in c, f"blocked container field '{field}' leaked"


def test_sanitize_pod_ready_false_when_any_container_not_ready() -> None:
    raw = dict(_RAW_POD)
    raw["containers"] = [
        {"name": "app", "ready": True, "restart_count": 0, "image": "app:v1"},
        {"name": "sidecar", "ready": False, "restart_count": 2, "image": "sidecar:v1"},
    ]
    out = _sanitize_pod(raw)
    assert out["ready"] is False
    assert out["restarts"] == 2


def test_sanitize_pod_ready_false_when_no_containers() -> None:
    raw = dict(_RAW_POD)
    raw["containers"] = []
    out = _sanitize_pod(raw)
    assert out["ready"] is False
    assert out["restarts"] == 0


def test_sanitize_pod_handles_missing_optional_fields() -> None:
    minimal = {"name": "pod-1", "phase": "Running"}
    out = _sanitize_pod(minimal)
    assert out["name"] == "pod-1"
    assert out["namespace"] == ""
    assert out["age"] == ""
    assert out["ready"] is False
    assert out["containers"] == []


# ---------------------------------------------------------------------------
# Integration: k8s_list_pods output shape
# ---------------------------------------------------------------------------


def test_k8s_list_pods_output_shape_via_sanitizer() -> None:
    """Simulate what k8s_list_pods returns for a two-pod namespace."""
    pods_raw = [_RAW_POD, {**_RAW_POD, "name": "incidentflow-mcp-76f5987dc5-k9p2x"}]
    sanitized = [_sanitize_pod(p) for p in pods_raw]
    output = {
        "status": "success",
        "data": {
            "pods": sanitized,
            "count": len(sanitized),
            "total": len(pods_raw),
            "truncated": False,
        },
        "error": None,
    }
    serialized = json.dumps(output)
    parsed = json.loads(serialized)

    assert parsed["data"]["count"] == 2
    for pod in parsed["data"]["pods"]:
        assert "labels" not in pod
        assert "annotations" not in pod
        assert "env" not in pod
        assert "volumes" not in pod
        assert "@sha256:" not in pod["containers"][0]["image"]
