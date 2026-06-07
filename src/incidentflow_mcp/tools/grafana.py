"""Grafana read tools for MCP.

These are thin relays over :class:`PlatformGrafanaClient`: platform-api performs
the dashboard allow-list check, PromQL validation, metric normalization and
label sanitization, so each tool here just calls the internal endpoint and
shapes the response into a stable, chat-safe output model.

The client is referenced via the :class:`GrafanaReadClient` protocol so the
tools are unit-testable with a fake and stay decoupled from the httpx client.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class GrafanaReadClient(Protocol):
    """Subset of ``PlatformGrafanaClient`` the tools depend on."""

    async def list_dashboards(self) -> list[dict[str, Any]]: ...
    async def get_dashboard(self, dashboard_uid: str) -> dict[str, Any]: ...
    async def extract_queries(self, dashboard_uid: str) -> list[dict[str, Any]]: ...
    async def query(
        self, *, datasource_uid: str, query: str, time: str | None = None
    ) -> dict[str, Any]: ...
    async def query_range(
        self, *, datasource_uid: str, query: str, start: str, end: str, step: str
    ) -> dict[str, Any]: ...
    async def analyze(
        self,
        *,
        dashboard_uid: str,
        start: str = "now-6h",
        end: str = "now",
        step: str | None = None,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Output models (mirror platform-api's grafana schemas; extra fields ignored)
# ---------------------------------------------------------------------------


class DashboardItem(BaseModel):
    uid: str
    title: str = ""
    folder: str | None = None
    tags: list[str] = Field(default_factory=list)
    datasource_uid: str | None = None
    enabled: bool = False


class ListDashboardsOutput(BaseModel):
    dashboards: list[DashboardItem] = Field(default_factory=list)
    returned: int = 0


class DashboardDetailOutput(BaseModel):
    # Platform passes dashboard metadata through; keep unknown fields.
    model_config = ConfigDict(extra="allow")

    uid: str = ""
    title: str = ""
    folder: str | None = None


class ExtractedQueryItem(BaseModel):
    panel_id: int | None = None
    panel_title: str = ""
    ref_id: str | None = None
    datasource_uid: str | None = None
    expr: str


class ExtractQueriesOutput(BaseModel):
    dashboard_uid: str
    queries: list[ExtractedQueryItem] = Field(default_factory=list)


class MetricSample(BaseModel):
    timestamp: float
    value: float


class MetricSeries(BaseModel):
    metric: dict[str, str] = Field(default_factory=dict)
    samples: list[MetricSample] = Field(default_factory=list)


class QueryOutput(BaseModel):
    datasource_uid: str = ""
    query: str = ""
    result_type: str = ""
    series: list[MetricSeries] = Field(default_factory=list)
    warning: str | None = None


class PanelAnalysis(BaseModel):
    panel_title: str = ""
    expr: str = ""
    datasource_uid: str | None = None
    result_type: str | None = None
    series: list[MetricSeries] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    warning: str | None = None


class AnalyzeOutput(BaseModel):
    dashboard_uid: str
    dashboard_title: str = ""
    time_range: str = ""
    panels: list[PanelAnalysis] = Field(default_factory=list)
    summary_hints: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def grafana_list_dashboards(client: GrafanaReadClient) -> ListDashboardsOutput:
    items = await client.list_dashboards()
    dashboards = [DashboardItem.model_validate(d) for d in items]
    return ListDashboardsOutput(dashboards=dashboards, returned=len(dashboards))


async def grafana_get_dashboard(
    client: GrafanaReadClient, *, dashboard_uid: str
) -> DashboardDetailOutput:
    payload = await client.get_dashboard(dashboard_uid)
    return DashboardDetailOutput.model_validate(payload)


async def grafana_extract_panel_queries(
    client: GrafanaReadClient, *, dashboard_uid: str
) -> ExtractQueriesOutput:
    items = await client.extract_queries(dashboard_uid)
    queries = [ExtractedQueryItem.model_validate(q) for q in items]
    return ExtractQueriesOutput(dashboard_uid=dashboard_uid, queries=queries)


async def grafana_metrics_query(
    client: GrafanaReadClient, *, datasource_uid: str, query: str, time: str | None = None
) -> QueryOutput:
    payload = await client.query(datasource_uid=datasource_uid, query=query, time=time)
    return QueryOutput.model_validate(payload)


async def grafana_metrics_query_range(
    client: GrafanaReadClient,
    *,
    datasource_uid: str,
    query: str,
    start: str,
    end: str,
    step: str,
) -> QueryOutput:
    payload = await client.query_range(
        datasource_uid=datasource_uid, query=query, start=start, end=end, step=step
    )
    return QueryOutput.model_validate(payload)


async def analyze_dashboard_health(
    client: GrafanaReadClient,
    *,
    dashboard_uid: str,
    start: str = "now-6h",
    end: str = "now",
    step: str | None = None,
) -> AnalyzeOutput:
    payload = await client.analyze(dashboard_uid=dashboard_uid, start=start, end=end, step=step)
    return AnalyzeOutput.model_validate(payload)
