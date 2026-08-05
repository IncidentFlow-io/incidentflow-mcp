from incidentflow_mcp.mcp.resources import _grafana_widget_meta


def test_grafana_widget_csp_keeps_browser_connects_closed() -> None:
    meta = _grafana_widget_meta("https://grafana.incidentflow.io/")

    assert meta["openai/widgetCSP"]["connect_domains"] == []
    assert meta["ui"]["csp"]["connectDomains"] == []


def test_grafana_widget_csp_allows_static_assets_and_grafana_origin() -> None:
    meta = _grafana_widget_meta("https://grafana.incidentflow.io/")

    assert meta["openai/widgetCSP"]["resource_domains"] == [
        "https://persistent.oaistatic.com",
        "https://grafana.incidentflow.io",
    ]
    assert meta["ui"]["csp"]["resourceDomains"] == [
        "https://persistent.oaistatic.com",
        "https://grafana.incidentflow.io",
    ]
