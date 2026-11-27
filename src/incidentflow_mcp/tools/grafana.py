"""Grafana read tools for MCP.

These are thin relays over :class:`PlatformGrafanaClient`: platform-api performs
the dashboard allow-list check, PromQL validation, metric normalization and
label sanitization, so each tool here just calls the internal endpoint and
shapes the response into a stable, chat-safe output model.

The client is referenced via the :class:`GrafanaReadClient` protocol so the
tools are unit-testable with a fake and stay decoupled from the httpx client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class GrafanaReadClient(Protocol):
    """Subset of ``PlatformGrafanaClient`` the tools depend on."""

    async def health(self) -> dict[str, Any]: ...
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
    async def get_panel_view(
        self,
        *,
        dashboard_uid: str,
        panel_id: int,
        start: str = "now-1h",
        end: str = "now",
        variables: dict[str, str | list[str]] | None = None,
        max_points: int = 300,
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


class GrafanaConnectionHealthOutput(BaseModel):
    """Read-only Grafana connection health (owned; strict)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["connected", "degraded", "offline", "not_configured", "unknown"]
    healthy: bool
    ok: bool
    message: str = ""
    datasources_found: int = 0
    source: str = ""
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


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
    model_config = ConfigDict(extra="allow")

    metric: dict[str, str] = Field(default_factory=dict)
    samples: list[MetricSample] = Field(default_factory=list)


class QueryOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    datasource_uid: str = ""
    query: str = ""
    result_type: str = ""
    series: list[MetricSeries] = Field(default_factory=list)
    warning: str | None = None


class PanelAnalysis(BaseModel):
    model_config = ConfigDict(extra="allow")

    panel_title: str = ""
    expr: str = ""
    datasource_uid: str | None = None
    result_type: str | None = None
    series: list[MetricSeries] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    warning: str | None = None


class AnalyzeOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    dashboard_uid: str
    dashboard_title: str = ""
    time_range: str = ""
    panels: list[PanelAnalysis] = Field(default_factory=list)
    summary_hints: list[str] = Field(default_factory=list)
    analysis_window: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Requested and analyzed period, period of the returned points, whether anomaly "
            "detection used the full series, and how returned points were reduced."
        ),
    )


class PanelViewDataPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: int


class PanelViewOutput(BaseModel):
    # Platform-api owns the full schema and aliases. MCP validates and relays the
    # contract without adding raw Grafana responses or secrets.
    model_config = ConfigDict(extra="allow")

    version: Literal["1"] = "1"
    panel: dict[str, Any]
    dashboard: dict[str, Any]
    source: dict[str, Any]
    visualization: dict[str, Any]
    timeRange: dict[str, Any]
    variables: dict[str, str | list[str]] = Field(default_factory=dict)
    series: list[dict[str, Any]] = Field(default_factory=list)
    data: list[PanelViewDataPoint] = Field(default_factory=list)
    annotations: list[dict[str, Any]] = Field(default_factory=list)
    links: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


ResponseMode = Literal["compact", "full"]


def _append_warning(payload: dict[str, Any], warning: str) -> None:
    warnings = payload.setdefault("warnings", [])
    if isinstance(warnings, list) and warning not in warnings:
        warnings.append(warning)


def _trim_samples(
    series: list[dict[str, Any]], *, max_points: int
) -> tuple[list[dict[str, Any]], bool]:
    truncated = False
    compact_series: list[dict[str, Any]] = []
    for item in series:
        compact = dict(item)
        samples = compact.get("samples")
        if isinstance(samples, list) and len(samples) > max_points:
            compact["samples"] = samples[-max_points:]
            compact["samples_returned"] = max_points
            compact["samples_total"] = len(samples)
            compact["samples_truncated"] = True
            truncated = True
        compact_series.append(compact)
    return compact_series, truncated


def _series_with_data_first(series: list[Any]) -> list[Any]:
    """Stable order that keeps series with samples ahead of empty ones.

    Trimming to ``max_series`` must not return only empty series while series
    with data exist further down the upstream list.
    """

    def has_samples(item: Any) -> bool:
        return isinstance(item, dict) and bool(item.get("samples"))

    return sorted(series, key=lambda item: not has_samples(item))


def _compact_query_payload(
    payload: dict[str, Any], *, max_series: int, max_points: int
) -> dict[str, Any]:
    compact = dict(payload)
    series = compact.get("series")
    if not isinstance(series, list):
        return compact

    total_series = len(series)
    bounded_series, samples_truncated = _trim_samples(
        _series_with_data_first(series)[:max_series], max_points=max_points
    )
    compact["series"] = bounded_series
    compact["series_returned"] = len(bounded_series)
    compact["series_total"] = compact.get("series_total", total_series)
    if total_series > max_series or samples_truncated:
        compact["truncated"] = True
        reason = f"Metric series trimmed to {max_series} series and {max_points} samples each."
        _append_warning(compact, reason)
    return compact


def _compact_dashboard_payload(payload: dict[str, Any], *, panel_limit: int) -> dict[str, Any]:
    compact = dict(payload)
    dashboard = compact.get("dashboard")
    if not isinstance(dashboard, dict):
        return compact

    if not compact.get("uid") and dashboard.get("uid"):
        compact["uid"] = dashboard.get("uid")
    if not compact.get("title") and dashboard.get("title"):
        compact["title"] = dashboard.get("title")
    if not compact.get("folder") and dashboard.get("folder"):
        compact["folder"] = dashboard.get("folder")

    panels = dashboard.get("panels")
    compact_dashboard = {
        key: dashboard.get(key)
        for key in ("uid", "title", "schemaVersion", "version", "refresh", "tags", "time")
        if key in dashboard
    }
    if isinstance(panels, list):
        compact_panels = []
        for panel in panels[:panel_limit]:
            if isinstance(panel, dict):
                compact_panels.append(
                    {
                        key: panel.get(key)
                        for key in ("id", "title", "type", "datasource", "targets")
                        if key in panel
                    }
                )
            else:
                compact_panels.append(panel)
        compact_dashboard["panels"] = compact_panels
        compact_dashboard["panels_returned"] = len(compact_panels)
        compact_dashboard["panels_total"] = len(panels)
        if len(panels) > panel_limit:
            compact["truncated"] = True
            _append_warning(compact, f"Dashboard panels trimmed to {panel_limit}.")
    compact["dashboard"] = compact_dashboard
    return compact


def _compact_analyze_payload(
    payload: dict[str, Any], *, panel_limit: int, max_series: int, max_points: int
) -> dict[str, Any]:
    compact = dict(payload)
    panels = compact.get("panels")
    if not isinstance(panels, list):
        return compact

    total_panels = len(panels)
    compact_panels: list[dict[str, Any]] = []
    truncated = total_panels > panel_limit
    for panel in panels[:panel_limit]:
        if not isinstance(panel, dict):
            continue
        panel_copy = dict(panel)
        series = panel_copy.get("series")
        if isinstance(series, list):
            panel_copy["series"], series_truncated = _trim_samples(
                _series_with_data_first(series)[:max_series], max_points=max_points
            )
            panel_copy["series_returned"] = len(panel_copy["series"])
            panel_copy["series_total"] = len(series)
            truncated = truncated or len(series) > max_series or series_truncated
        compact_panels.append(panel_copy)

    compact["panels"] = compact_panels
    compact["panels_returned"] = len(compact_panels)
    compact["panels_total"] = total_panels
    if truncated:
        compact["summary_hints"] = _analyze_summary_hints(compact_panels)
        compact["truncated"] = True
        _append_warning(
            compact,
            (
                f"Dashboard analysis trimmed to {panel_limit} panels, {max_series} series "
                f"per panel, and {max_points} samples per series."
            ),
        )
    return compact


def _sample_span(payload: Any) -> dict[str, Any]:
    """First/last sample timestamp and point count over every panel series."""

    first: float | None = None
    last: float | None = None
    points = 0
    panels = payload.get("panels") if isinstance(payload, dict) else None
    for panel in panels if isinstance(panels, list) else []:
        series = panel.get("series") if isinstance(panel, dict) else None
        for item in series if isinstance(series, list) else []:
            samples = item.get("samples") if isinstance(item, dict) else None
            for sample in samples if isinstance(samples, list) else []:
                stamp = sample.get("timestamp") if isinstance(sample, dict) else None
                if not isinstance(stamp, (int, float)):
                    continue
                points += 1
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)

    def iso(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")

    return {"start": iso(first), "end": iso(last), "points": points}


def _analysis_window(
    upstream: Any,
    returned: dict[str, Any],
    *,
    start: str,
    end: str,
    step: str | None,
    response_mode: ResponseMode,
    panel_limit: int,
    max_series: int,
    max_points: int,
) -> dict[str, Any]:
    """Say which period was analyzed and how the returned points were reduced.

    platform-api runs each panel query over the full requested range and flags
    anomalies on every series and sample it got back; MCP trims only the
    returned copy afterwards. Anomalies can therefore mention series or points
    that are not in the returned ``series``.
    """

    reduced = response_mode == "compact" and bool(returned.get("truncated"))
    return {
        "requested_range": {"start": start, "end": end},
        "step": step,
        "step_source": "requested" if step else "platform_default",
        "analyzed_range": {
            "expression": upstream.get("time_range") if isinstance(upstream, dict) else None,
            **_sample_span(upstream),
        },
        "returned_range": _sample_span(returned),
        "anomaly_detection": "full_series",
        "anomaly_detection_note": (
            "Anomalies were computed on every series and every sample returned by each "
            "panel query, before any trimming; they may refer to series or points that "
            "are not included in the returned series."
        ),
        "point_reduction": (
            {
                "applied": True,
                "panels": f"first {panel_limit}",
                "series": f"first {max_series} per panel",
                "samples": (
                    f"latest {max_points} per series (older samples dropped, no downsampling)"
                ),
            }
            if reduced
            else {"applied": False}
        ),
    }


def _analyze_summary_hints(panels: list[dict[str, Any]]) -> list[str]:
    if not panels:
        return ["no Prometheus panels found on this dashboard"]
    rejected = sum(
        1
        for panel in panels
        if isinstance(panel.get("warning"), str) and panel["warning"].startswith("rejected")
    )
    failed = sum(
        1
        for panel in panels
        if isinstance(panel.get("warning"), str) and panel["warning"].startswith("query failed")
    )
    with_anomalies = sum(1 for panel in panels if panel.get("anomalies"))
    return [
        f"{len(panels)} panel queries analyzed",
        f"{rejected} rejected by guardrails",
        f"{failed} failed to query",
        f"{with_anomalies} with anomalies flagged",
    ]


def _with_panel_view_cardinality(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    series = compact.get("series")
    data = compact.get("data")

    if isinstance(series, list):
        compact.setdefault("series_returned", len(series))
        compact.setdefault("series_total", len(series))
    if isinstance(data, list):
        compact.setdefault("samples_returned", len(data))
        compact.setdefault("samples_total", len(data))
    compact.setdefault("truncated", bool(compact.get("warnings")))
    return compact


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def grafana_connection_health(client: GrafanaReadClient) -> GrafanaConnectionHealthOutput:
    payload = await client.health()
    ok = bool(payload.get("ok"))
    message = str(payload.get("message") or "")
    if message == "grafana_not_configured":
        status = "not_configured"
    elif ok:
        status = "connected"
    elif message in {
        "datasource_query_test_failed",
        "grafana_query_failed",
        "grafana_no_datasources",
    }:
        status = "degraded"
    else:
        status = "unknown"
    return GrafanaConnectionHealthOutput(
        status=status,
        healthy=ok,
        ok=ok,
        message=message,
        datasources_found=int(payload.get("datasources_found") or 0),
        source=str(payload.get("source") or ""),
        truncated=bool(payload.get("truncated")),
        warnings=list(payload.get("warnings") or []),
    )


async def grafana_list_dashboards(client: GrafanaReadClient) -> ListDashboardsOutput:
    items = await client.list_dashboards()
    dashboards = [DashboardItem.model_validate(d) for d in items]
    return ListDashboardsOutput(dashboards=dashboards, returned=len(dashboards))


async def grafana_get_dashboard(
    client: GrafanaReadClient,
    *,
    dashboard_uid: str,
    response_mode: ResponseMode = "compact",
    panel_limit: int = 20,
) -> DashboardDetailOutput:
    payload = await client.get_dashboard(dashboard_uid)
    if response_mode == "compact":
        payload = _compact_dashboard_payload(payload, panel_limit=panel_limit)
    return DashboardDetailOutput.model_validate(payload)


async def grafana_extract_panel_queries(
    client: GrafanaReadClient, *, dashboard_uid: str
) -> ExtractQueriesOutput:
    items = await client.extract_queries(dashboard_uid)
    queries = [ExtractedQueryItem.model_validate(q) for q in items]
    return ExtractQueriesOutput(dashboard_uid=dashboard_uid, queries=queries)


async def grafana_metrics_query(
    client: GrafanaReadClient,
    *,
    datasource_uid: str,
    query: str,
    time: str | None = None,
    response_mode: ResponseMode = "compact",
    max_series: int = 20,
    max_points: int = 120,
) -> QueryOutput:
    payload = await client.query(datasource_uid=datasource_uid, query=query, time=time)
    if response_mode == "compact":
        payload = _compact_query_payload(payload, max_series=max_series, max_points=max_points)
    return QueryOutput.model_validate(payload)


async def grafana_metrics_query_range(
    client: GrafanaReadClient,
    *,
    datasource_uid: str,
    query: str,
    start: str,
    end: str,
    step: str,
    response_mode: ResponseMode = "compact",
    max_series: int = 20,
    max_points: int = 120,
) -> QueryOutput:
    payload = await client.query_range(
        datasource_uid=datasource_uid, query=query, start=start, end=end, step=step
    )
    if response_mode == "compact":
        payload = _compact_query_payload(payload, max_series=max_series, max_points=max_points)
    return QueryOutput.model_validate(payload)


async def analyze_dashboard_health(
    client: GrafanaReadClient,
    *,
    dashboard_uid: str,
    start: str = "now-6h",
    end: str = "now",
    step: str | None = None,
    response_mode: ResponseMode = "compact",
    panel_limit: int = 10,
    max_series: int = 20,
    max_points: int = 120,
) -> AnalyzeOutput:
    upstream = await client.analyze(dashboard_uid=dashboard_uid, start=start, end=end, step=step)
    payload = upstream
    if response_mode == "compact":
        payload = _compact_analyze_payload(
            upstream, panel_limit=panel_limit, max_series=max_series, max_points=max_points
        )
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["analysis_window"] = _analysis_window(
            upstream,
            payload,
            start=start,
            end=end,
            step=step,
            response_mode=response_mode,
            panel_limit=panel_limit,
            max_series=max_series,
            max_points=max_points,
        )
    return AnalyzeOutput.model_validate(payload)


async def grafana_get_panel_view(
    client: GrafanaReadClient,
    *,
    dashboard_uid: str,
    panel_id: int,
    start: str = "now-1h",
    end: str = "now",
    variables: dict[str, str | list[str]] | None = None,
    max_points: int = 300,
) -> PanelViewOutput:
    payload = await client.get_panel_view(
        dashboard_uid=dashboard_uid,
        panel_id=panel_id,
        start=start,
        end=end,
        variables=variables or {},
        max_points=max_points,
    )
    payload = _with_panel_view_cardinality(payload)
    return PanelViewOutput.model_validate(payload)
