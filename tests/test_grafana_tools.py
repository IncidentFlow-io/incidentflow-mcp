"""Unit tests for the Grafana MCP read tools (fake client, no network)."""

from __future__ import annotations

from typing import Any

from incidentflow_mcp.tools.grafana import (
    _dns_summary_hints,
    _join_limited,
    analyze_dashboard_health,
    analyze_dns_dashboard,
    grafana_extract_panel_queries,
    grafana_get_dashboard,
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


class TestAnalyzeDnsDashboard:
    async def test_adds_dns_panel_and_error_hints(self) -> None:
        client = FakeClient(
            analyze={
                "dashboard_uid": "dns",
                "dashboard_title": "DNS",
                "time_range": "now-6h..now",
                "panels": [
                    {
                        "panel_title": "DNS Errors",
                        "expr": "sum by (rcode) (rate(coredns_dns_responses_total[5m]))",
                        "series": [
                            {
                                "metric": {"rcode": "SERVFAIL"},
                                "samples": [{"timestamp": 1.0, "value": 2.0}],
                            },
                            {
                                "metric": {"rcode": "NOERROR"},
                                "samples": [{"timestamp": 1.0, "value": 10.0}],
                            },
                        ],
                    },
                    {
                        "panel_title": "Other",
                        "expr": "up",
                        "series": [
                            {
                                "metric": {"rcode": "NXDOMAIN"},
                                "samples": [{"timestamp": 1.0, "value": 0.0}],
                            }
                        ],
                    },
                ],
                "summary_hints": ["2 panel queries analyzed"],
            }
        )

        out = await analyze_dns_dashboard(client, dashboard_uid="dns")

        assert out.summary_hints == [
            "2 panel queries analyzed",
            "DNS-focused panels detected: DNS Errors",
            "DNS error response samples above zero: SERVFAIL (DNS Errors)",
        ]
        assert client.calls == [
            (
                "analyze",
                {"dashboard_uid": "dns", "start": "now-6h", "end": "now", "step": None},
            )
        ]

    async def test_reports_no_dns_panels(self) -> None:
        out = await analyze_dns_dashboard(
            FakeClient(
                analyze={
                    "dashboard_uid": "api",
                    "panels": [{"panel_title": "API", "expr": "up", "series": []}],
                    "summary_hints": [],
                }
            ),
            dashboard_uid="api",
            start="now-1h",
            step="30s",
        )

        assert out.summary_hints == ["No DNS-focused panels detected by expression markers"]


class TestDnsSummaryHelpers:
    async def test_join_limited_deduplicates_and_limits(self) -> None:
        assert (
            _join_limited(["a", "b", "a", "c", "d", "e", "f", "g"], limit=5)
            == "a, b, c, d, e, +2 more"
        )

    async def test_dns_summary_hints_includes_limited_dns_panels(self) -> None:
        payload = {
            "dashboard_uid": "dns",
            "dashboard_title": "DNS",
            "panels": [
                {"panel_title": f"DNS panel {i}", "expr": "coredns_dns_requests_total"}
                for i in range(1, 8)
            ],
            "summary_hints": [],
        }
        out = await analyze_dns_dashboard(FakeClient(analyze=payload), dashboard_uid="dns")

        assert out.summary_hints == [
            "DNS-focused panels detected: DNS panel 1, DNS panel 2, DNS panel 3, "
            "DNS panel 4, DNS panel 5, +2 more"
        ]

    async def test_dns_summary_includes_no_dns_panel_warning(self) -> None:
        hints = _dns_summary_hints([])
        assert hints == ["No DNS-focused panels detected by expression markers"]
