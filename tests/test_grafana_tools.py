"""Unit tests for the Grafana MCP read tools (fake client, no network)."""

from __future__ import annotations

from typing import Any

from incidentflow_mcp.tools.grafana import (
    analyze_dashboard_health,
    grafana_extract_panel_queries,
    grafana_get_dashboard,
    grafana_get_panel_view,
    grafana_list_dashboards,
    grafana_metrics_query,
    grafana_metrics_query_range,
)


class FakeClient:
    """Duck-typed GrafanaReadClient that records calls and returns canned payloads."""

    def __init__(self, **payloads: Any) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_dashboards(self) -> list[dict[str, Any]]:
        self.calls.append(("list_dashboards", {}))
        return self._payloads.get("list_dashboards", [])

    async def get_dashboard(self, dashboard_uid: str) -> dict[str, Any]:
        self.calls.append(("get_dashboard", {"dashboard_uid": dashboard_uid}))
        return self._payloads.get("get_dashboard", {})

    async def extract_queries(self, dashboard_uid: str) -> list[dict[str, Any]]:
        self.calls.append(("extract_queries", {"dashboard_uid": dashboard_uid}))
        return self._payloads.get("extract_queries", [])

    async def query(
        self, *, datasource_uid: str, query: str, time: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("query", {"datasource_uid": datasource_uid, "query": query, "time": time})
        )
        return self._payloads.get("query", {})

    async def query_range(
        self, *, datasource_uid: str, query: str, start: str, end: str, step: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "query_range",
                {
                    "datasource_uid": datasource_uid,
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step,
                },
            )
        )
        return self._payloads.get("query_range", {})

    async def analyze(
        self,
        *,
        dashboard_uid: str,
        start: str = "now-6h",
        end: str = "now",
        step: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("analyze", {"dashboard_uid": dashboard_uid, "start": start, "end": end, "step": step})
        )
        return self._payloads.get("analyze", {})

    async def get_panel_view(
        self,
        *,
        dashboard_uid: str,
        panel_id: int,
        start: str = "now-1h",
        end: str = "now",
        variables: dict[str, str | list[str]] | None = None,
        max_points: int = 300,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_panel_view",
                {
                    "dashboard_uid": dashboard_uid,
                    "panel_id": panel_id,
                    "start": start,
                    "end": end,
                    "variables": variables or {},
                    "max_points": max_points,
                },
            )
        )
        return self._payloads.get("get_panel_view", {})


class TestListDashboards:
    async def test_maps_items_and_counts(self) -> None:
        client = FakeClient(
            list_dashboards=[
                {"uid": "a", "title": "DNS", "enabled": True, "tags": ["dns"]},
                {"uid": "b", "title": "API"},
            ]
        )
        out = await grafana_list_dashboards(client)
        assert out.returned == 2
        assert out.dashboards[0].enabled is True
        assert out.dashboards[0].tags == ["dns"]
        assert out.dashboards[1].enabled is False

    async def test_empty(self) -> None:
        out = await grafana_list_dashboards(FakeClient())
        assert out.returned == 0
        assert out.dashboards == []


class TestGetDashboard:
    async def test_passthrough_keeps_unknown_fields(self) -> None:
        client = FakeClient(
            get_dashboard={"uid": "dns", "title": "DNS", "panels": [{"id": 1}], "schemaVersion": 39}
        )
        out = await grafana_get_dashboard(client, dashboard_uid="dns")
        assert out.uid == "dns"
        dumped = out.model_dump()
        assert dumped["panels"] == [{"id": 1}]
        assert dumped["schemaVersion"] == 39
        assert client.calls == [("get_dashboard", {"dashboard_uid": "dns"})]

    async def test_compact_mode_trims_dashboard_panels(self) -> None:
        client = FakeClient(
            get_dashboard={
                "uid": "dns",
                "title": "DNS",
                "dashboard": {
                    "uid": "dns",
                    "title": "DNS",
                    "panels": [
                        {"id": 1, "title": "A", "type": "timeseries", "gridPos": {"x": 0}},
                        {"id": 2, "title": "B", "type": "stat", "gridPos": {"x": 1}},
                    ],
                },
            }
        )

        out = await grafana_get_dashboard(client, dashboard_uid="dns", panel_limit=1)
        payload = out.model_dump()

        assert payload["truncated"] is True
        assert payload["dashboard"]["panels_returned"] == 1
        assert payload["dashboard"]["panels_total"] == 2
        assert payload["dashboard"]["panels"][0] == {"id": 1, "title": "A", "type": "timeseries"}
        assert "Dashboard panels trimmed to 1." in payload["warnings"]


class TestExtractQueries:
    async def test_maps_queries(self) -> None:
        client = FakeClient(
            extract_queries=[
                {"panel_title": "CPU", "expr": "node_cpu", "ref_id": "A", "datasource_uid": "ds1"}
            ]
        )
        out = await grafana_extract_panel_queries(client, dashboard_uid="dns")
        assert out.dashboard_uid == "dns"
        assert out.queries[0].expr == "node_cpu"
        assert out.queries[0].panel_id is None


class TestMetricsQuery:
    async def test_instant_query_shapes_series(self) -> None:
        client = FakeClient(
            query={
                "datasource_uid": "ds1",
                "query": "up",
                "result_type": "vector",
                "series": [
                    {"metric": {"job": "node"}, "samples": [{"timestamp": 1.0, "value": 1.0}]},
                ],
            }
        )
        out = await grafana_metrics_query(client, datasource_uid="ds1", query="up", time="123")
        assert out.result_type == "vector"
        assert out.series[0].metric == {"job": "node"}
        assert out.series[0].samples[0].value == 1.0
        assert client.calls[0][1]["time"] == "123"

    async def test_instant_query_compact_mode_trims_series_and_samples(self) -> None:
        client = FakeClient(
            query={
                "datasource_uid": "ds1",
                "query": "up",
                "result_type": "vector",
                "series": [
                    {
                        "metric": {"job": "a"},
                        "samples": [
                            {"timestamp": 1.0, "value": 1.0},
                            {"timestamp": 2.0, "value": 2.0},
                        ],
                    },
                    {
                        "metric": {"job": "b"},
                        "samples": [{"timestamp": 1.0, "value": 1.0}],
                    },
                ],
            }
        )

        out = await grafana_metrics_query(
            client,
            datasource_uid="ds1",
            query="up",
            max_series=1,
            max_points=1,
        )
        payload = out.model_dump()

        assert payload["truncated"] is True
        assert payload["series_returned"] == 1
        assert payload["series_total"] == 2
        assert len(payload["series"]) == 1
        assert payload["series"][0]["samples"][0]["timestamp"] == 2.0
        assert payload["series"][0]["samples_truncated"] is True

    async def test_range_query_passthrough(self) -> None:
        client = FakeClient(
            query_range={
                "datasource_uid": "ds1",
                "query": "up",
                "result_type": "matrix",
                "series": [],
            }
        )
        out = await grafana_metrics_query_range(
            client,
            datasource_uid="ds1",
            query="up",
            start="now-6h",
            end="now",
            step="60s",
        )
        assert out.result_type == "matrix"
        assert client.calls[0][1]["step"] == "60s"

    async def test_range_query_compact_mode_trims_series_and_samples(self) -> None:
        client = FakeClient(
            query_range={
                "datasource_uid": "ds1",
                "query": "up",
                "result_type": "matrix",
                "series": [
                    {
                        "metric": {"job": "a"},
                        "samples": [
                            {"timestamp": 1.0, "value": 1.0},
                            {"timestamp": 2.0, "value": 2.0},
                        ],
                    },
                    {
                        "metric": {"job": "b"},
                        "samples": [{"timestamp": 1.0, "value": 1.0}],
                    },
                ],
            }
        )

        out = await grafana_metrics_query_range(
            client,
            datasource_uid="ds1",
            query="up",
            start="now-6h",
            end="now",
            step="60s",
            max_series=1,
            max_points=1,
        )
        payload = out.model_dump()

        assert payload["truncated"] is True
        assert payload["series_returned"] == 1
        assert payload["series_total"] == 2
        assert len(payload["series"]) == 1
        assert payload["series"][0]["samples"][0]["timestamp"] == 2.0
        assert payload["series"][0]["samples_truncated"] is True


class TestAnalyze:
    async def test_analyze_maps_panels_and_hints(self) -> None:
        client = FakeClient(
            analyze={
                "dashboard_uid": "dns",
                "dashboard_title": "DNS",
                "time_range": "now-6h..now",
                "panels": [
                    {
                        "panel_title": "QPS",
                        "expr": "coredns_dns_requests_total",
                        "result_type": "matrix",
                        "anomalies": ["spike at 12:00"],
                    }
                ],
                "summary_hints": ["1 panel queries analyzed"],
            }
        )
        out = await analyze_dashboard_health(client, dashboard_uid="dns")
        assert out.dashboard_title == "DNS"
        assert out.panels[0].anomalies == ["spike at 12:00"]
        assert out.summary_hints == ["1 panel queries analyzed"]
        assert client.calls[0][1] == {
            "dashboard_uid": "dns",
            "start": "now-6h",
            "end": "now",
            "step": None,
        }

    async def test_analyze_forwards_window(self) -> None:
        out = await analyze_dashboard_health(
            FakeClient(analyze={"dashboard_uid": "dns"}),
            dashboard_uid="dns",
            start="now-1h",
            end="now",
            step="30s",
        )
        assert out.dashboard_uid == "dns"
        assert out.panels == []

    async def test_json_serializable(self) -> None:
        out = await analyze_dashboard_health(
            FakeClient(analyze={"dashboard_uid": "dns", "panels": []}), dashboard_uid="dns"
        )
        # Tools serialize via model_dump_json in the server layer.
        assert '"dashboard_uid":"dns"' in out.model_dump_json()

    async def test_analyze_compact_mode_trims_panels_series_and_samples(self) -> None:
        client = FakeClient(
            analyze={
                "dashboard_uid": "dns",
                "panels": [
                    {
                        "panel_title": "A",
                        "series": [
                            {
                                "metric": {"job": "a"},
                                "samples": [
                                    {"timestamp": 1.0, "value": 1.0},
                                    {"timestamp": 2.0, "value": 2.0},
                                ],
                            },
                            {
                                "metric": {"job": "b"},
                                "samples": [{"timestamp": 1.0, "value": 1.0}],
                            },
                        ],
                    },
                    {"panel_title": "B", "series": []},
                ],
            }
        )

        out = await analyze_dashboard_health(
            client,
            dashboard_uid="dns",
            panel_limit=1,
            max_series=1,
            max_points=1,
        )
        payload = out.model_dump()

        assert payload["truncated"] is True
        assert payload["panels_returned"] == 1
        assert payload["panels_total"] == 2
        assert payload["panels"][0]["series_returned"] == 1
        assert payload["panels"][0]["series_total"] == 2
        assert payload["panels"][0]["series"][0]["samples_truncated"] is True


class TestPanelView:
    async def test_panel_view_maps_and_forwards(self) -> None:
        client = FakeClient(
            get_panel_view={
                "version": "1",
                "panel": {"id": 7, "title": "Request rate", "type": "timeseries"},
                "dashboard": {"uid": "platform", "title": "Platform"},
                "source": {"type": "grafana", "datasourceUid": "prom"},
                "visualization": {
                    "type": "line",
                    "stacked": False,
                    "showLegend": True,
                    "showTooltip": True,
                },
                "timeRange": {"from": 1000, "to": 2000},
                "variables": {"service": "platform-api"},
                "series": [{"key": "series_0", "name": "api"}],
                "data": [{"timestamp": 1000, "series_0": 1.0}],
                "annotations": [],
                "links": {"grafana": "https://grafana.test/d/platform/platform?viewPanel=7"},
                "warnings": [],
            }
        )

        out = await grafana_get_panel_view(
            client,
            dashboard_uid="platform",
            panel_id=7,
            start="now-1h",
            end="now",
            variables={"service": "platform-api"},
            max_points=200,
        )

        assert out.panel["title"] == "Request rate"
        assert out.source["datasourceUid"] == "prom"
        assert out.data[0].model_extra == {"series_0": 1.0}
        assert client.calls[0][0] == "get_panel_view"
        assert client.calls[0][1]["max_points"] == 200
