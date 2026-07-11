import { describe, expect, it } from "vitest";
import { grafanaPanelViewSchema } from "./grafana-panel-view";

const validPanelView = {
  version: "1",
  panel: { id: 7, title: "Request rate", type: "timeseries", unit: "reqps" },
  dashboard: { uid: "platform", title: "Platform API" },
  source: { type: "grafana", datasourceUid: "prom" },
  visualization: { type: "line", stacked: false, showLegend: true, showTooltip: true },
  timeRange: { from: 1000, to: 2000 },
  variables: { service: "platform-api" },
  series: [{ key: "series_0", name: "api", unit: "reqps" }],
  data: [{ timestamp: 1000, series_0: 1 }],
  annotations: [{ id: "spike-1", type: "spike", timestamp: 1500, label: "Spike", value: 12 }],
  links: { grafana: "https://grafana.incidentflow.io/d/platform/platform-api?viewPanel=7" },
  warnings: []
};

describe("grafanaPanelViewSchema", () => {
  it("accepts a valid panel view", () => {
    expect(grafanaPanelViewSchema.parse(validPanelView).panel.title).toBe("Request rate");
  });

  it("rejects unsupported panel types", () => {
    expect(
      grafanaPanelViewSchema.safeParse({
        ...validPanelView,
        panel: { ...validPanelView.panel, type: "stat" }
      }).success
    ).toBe(false);
  });
});
