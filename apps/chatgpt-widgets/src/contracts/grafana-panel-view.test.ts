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

const memoryBasicPanelView = {
  version: "1",
  panel: {
    id: 78,
    title: "Memory Basic",
    type: "timeseries",
    unit: "bytes"
  },
  dashboard: {
    uid: "rYdddlPWk",
    title: "Node Exporter Full"
  },
  source: {
    type: "grafana",
    datasourceUid: "PBFA97CFB590B2093"
  },
  visualization: {
    type: "area",
    stacked: true,
    showLegend: true,
    showTooltip: true
  },
  timeRange: {
    from: 1783760400000,
    to: 1783764000000
  },
  variables: {
    job: "kubernetes-service-endpoints",
    node: "178.238.230.95:9100",
    nodename: "vmi3338759"
  },
  series: [
    {
      key: "series_0",
      name: "Total"
    },
    {
      key: "series_1",
      name: "Used"
    },
    {
      key: "series_2",
      name: "Cache + Buffer"
    },
    {
      key: "series_3",
      name: "Free"
    },
    {
      key: "series_4",
      name: "Swap Used"
    }
  ],
  data: [
    {
      timestamp: 1783760400000,
      series_0: 12536139776,
      series_1: 2982146048,
      series_2: 1673527296,
      series_3: 7880466432,
      series_4: 0
    },
    {
      timestamp: 1783760460000,
      series_0: 12536139776,
      series_1: 3015702528,
      series_2: 1690304512,
      series_3: 7846912000,
      series_4: 0
    },
    {
      timestamp: 1783760520000,
      series_0: 12536139776,
      series_1: 3049259008,
      series_2: 1707081728,
      series_3: 7813357568,
      series_4: 0
    }
  ],
  annotations: [],
  links: {
    grafana: "https://grafana.incidentflow.io/d/rYdddlPWk/node-exporter-full?viewPanel=78"
  },
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

  it("accepts memory basic payload with series_N datapoints", () => {
    expect(grafanaPanelViewSchema.parse(memoryBasicPanelView).panel.title).toBe("Memory Basic");
  });

  it("accepts null Grafana series colors", () => {
    const result = grafanaPanelViewSchema.safeParse({
      ...memoryBasicPanelView,
      series: memoryBasicPanelView.series.map((series) => ({
        ...series,
        unit: "bytes",
        color: null
      }))
    });

    expect(result.success).toBe(true);
  });

  it("accepts sparse annotations with nullable bounds", () => {
    const result = grafanaPanelViewSchema.safeParse({
      ...memoryBasicPanelView,
      annotations: [
        {
          id: "spike-series_1-1783798200000",
          type: "spike",
          timestamp: 1783798200000,
          from: null,
          to: null,
          label: "Spike",
          value: null
        }
      ]
    });

    expect(result.success).toBe(true);
  });

  it("accepts Grafana panels with nullable optional text metadata", () => {
    const result = grafanaPanelViewSchema.safeParse({
      ...validPanelView,
      panel: {
        ...validPanelView.panel,
        description: null,
        unit: null
      },
      series: validPanelView.series.map((series) => ({
        ...series,
        unit: null
      }))
    });

    expect(result.success).toBe(true);
  });
});
