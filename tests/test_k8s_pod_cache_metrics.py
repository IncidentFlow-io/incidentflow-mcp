import json
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.registration import kubernetes


async def _guard(_tool):
    return None


def _registered_tools():
    registered = {}

    def tool(**_metadata):
        def capture(fn):
            registered[fn.__name__] = fn
            return fn

        return capture

    ctx = SimpleNamespace(
        settings=Settings(), mcp=SimpleNamespace(tool=tool), metadata=lambda _: {}
    )
    kubernetes.register_kubernetes_tools(
        ctx, memory=None, resolve_tool_guard=_guard, current_bearer_token=lambda: "test"
    )
    return registered


@pytest.mark.asyncio
async def test_pod_list_latency_uses_agent_outcome_and_forwards_freshness(monkeypatch):
    registered = _registered_tools()
    cases = iter(
        [
            ({"namespace": "shop"}, {"cache_result": "cached"}),
            (
                {"namespace": "shop", "consistency": "fresh", "max_staleness_seconds": 3},
                {"cache_result": "live"},
            ),
            ({"namespace": "shop"}, {}),
        ]
    )

    async def send(**kwargs):
        params, metadata = next(cases)
        assert kwargs["params"] == params
        return json.dumps(
            {
                "status": "succeeded",
                "data": {"pods": [], **metadata},
            }
        )

    monkeypatch.setattr(kubernetes, "_send_k8s_agent_command", send)
    metric = "incidentflow_mcp_kubernetes_tool_duration_seconds_count"

    def counts():
        return [
            REGISTRY.get_sample_value(metric, {"cache_result": result}) or 0
            for result in ("cached", "live")
        ]

    before = counts()
    for options in ({}, {"consistency": "fresh", "max_staleness_seconds": 3}, {}):
        result = await registered["k8s_list_pods"](namespace="shop", **options)
        assert result["data"]["pods"] == []
    assert counts() == [value + 1 for value in before]


@pytest.mark.asyncio
async def test_unhealthy_tool_requests_health_index_and_preserves_live_errors(monkeypatch):
    registered = _registered_tools()
    unavailable = {"status": "failed", "error": {"code": "unavailable", "message": "API offline"}}
    responses = iter(
        [
            {
                "status": "succeeded",
                "data": {
                    "pods": [
                        {"name": "healthy", "phase": "Running"},
                        {"name": "waiting", "phase": "Pending"},
                        {"name": "completed", "phase": "Succeeded"},
                    ]
                },
            },
            unavailable,
        ]
    )

    async def send(**kwargs):
        assert kwargs["params"] == {"namespace": "shop", "health_summary_only": True}
        return json.dumps(next(responses))

    monkeypatch.setattr(kubernetes, "_send_k8s_agent_command", send)
    result = await registered["k8s_show_unhealthy_pods"](namespace="shop")
    assert [pod["name"] for pod in result["data"]["unhealthy_pods"]] == ["waiting"]
    assert result["data"]["completed_count"] == 1
    assert await registered["k8s_show_unhealthy_pods"](namespace="shop") == unavailable
