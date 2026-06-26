from __future__ import annotations

from typing import Any

import httpx

from incidentflow_mcp.config import Settings
from incidentflow_mcp.observability.tracing import get_tracer, inject_trace_headers


class PlatformAPIAgentCommandsClient:
    """Thin client for Kubernetes agent command dispatch through platform-api."""

    def __init__(self, settings: Settings) -> None:
        if not settings.platform_api_base_url:
            raise ValueError("PLATFORM_API_BASE_URL is required for Kubernetes agent tools")
        self._base_url = settings.platform_api_base_url.rstrip("/")
        self._timeout = settings.platform_api_timeout_seconds

    async def list_clusters(self, *, bearer_token: str) -> list[dict[str, Any]]:
        tracer = get_tracer()
        with tracer.start_as_current_span("platform_api.agent_lookup") as span:
            span.set_attribute("http.method", "GET")
            span.set_attribute("http.url", f"{self._base_url}/api/v1/agents/clusters")
            headers: dict[str, str] = {"Authorization": f"Bearer {bearer_token}"}
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(
                        f"{self._base_url}/api/v1/agents/clusters",
                        headers=headers,
                    )
                response.raise_for_status()
                payload = response.json()
                clusters = payload.get("clusters") if isinstance(payload, dict) else None
                result = clusters if isinstance(clusters, list) else []
                span.set_attribute("clusters.count", len(result))
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("response.bytes", len(response.content))
                online = sum(1 for c in result if c.get("connected"))
                span.set_attribute("clusters.online", online)
                return result
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise

    async def dispatch(
        self,
        *,
        bearer_token: str,
        cluster_id: str,
        action: str,
        params: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        import json as _json
        tracer = get_tracer()
        with tracer.start_as_current_span("platform_api.dispatch_command") as span:
            span.set_attribute("k8s.command", action)
            span.set_attribute("cluster.id", cluster_id)
            span.set_attribute("http.method", "POST")
            span.set_attribute("http.url", f"{self._base_url}/api/v1/agents/clusters/{cluster_id}/commands")
            if timeout_seconds is not None:
                span.set_attribute("agent_command.timeout_seconds", timeout_seconds)
            if "namespace" in params:
                span.set_attribute("k8s.namespace.name", str(params["namespace"]))
            if "pod" in params:
                span.set_attribute("k8s.pod.name", str(params["pod"]))
            if "deployment" in params:
                span.set_attribute("k8s.deployment.name", str(params["deployment"]))

            req_payload: dict[str, Any] = {"action": action, "params": params}
            if timeout_seconds is not None:
                req_payload["timeout_seconds"] = timeout_seconds

            req_bytes = len(_json.dumps(req_payload).encode())
            span.set_attribute("request.bytes", req_bytes)

            headers: dict[str, str] = {"Authorization": f"Bearer {bearer_token}"}
            inject_trace_headers(headers)
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout + (timeout_seconds or 0)
                ) as client:
                    response = await client.post(
                        f"{self._base_url}/api/v1/agents/clusters/{cluster_id}/commands",
                        headers=headers,
                        json=req_payload,
                    )
                response.raise_for_status()
                result = response.json()
                status = result.get("status", "unknown")
                span.set_attribute("agent_command.status", str(status))
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("response.bytes", len(response.content))
                if result.get("data") and isinstance(result["data"], dict):
                    items = result["data"].get("items") or result["data"].get("pods") or []
                    if isinstance(items, list):
                        span.set_attribute("response.items", len(items))
                try:
                    from opentelemetry.trace import StatusCode
                    if status == "succeeded":
                        span.set_status(StatusCode.OK)
                    else:
                        span.set_status(StatusCode.ERROR, status)
                except Exception:
                    pass
                return result
            except Exception as exc:
                try:
                    from opentelemetry.trace import StatusCode
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                except Exception:
                    pass
                raise
