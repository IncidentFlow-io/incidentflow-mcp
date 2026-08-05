import { describe, expect, it } from "vitest";
import { grafanaPanelViewSchema } from "../contracts/grafana-panel-view";
import { getPanelPayload, selectPanelPayload } from "./payload";

const memoryBasicPanelView = {
  version: "1",
  panel: {
    id: 78,
    title: "Memory Basic",
    description: "Basic memory usage",
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
    ds_prometheus: "PBFA97CFB590B2093",
    job: "kubernetes-service-endpoints",
    node: "178.238.230.95:9100",
    nodename: "vmi3338759"
  },
  series: [
    { key: "series_0", name: "Total", unit: "bytes", color: null },
    { key: "series_1", name: "Used", unit: "bytes", color: null },
    { key: "series_2", name: "Cache + Buffer", unit: "bytes", color: null },
    { key: "series_3", name: "Free", unit: "bytes", color: null },
    { key: "series_4", name: "Swap used", unit: "bytes", color: null }
  ],
  data: [
    {
      timestamp: 1783760400000,
      series_0: 12536139776,
      series_1: 2982146048,
      series_2: 1673527296,
      series_3: 7880466432,
      series_4: 0
    }
  ],
  annotations: [],
  links: {
    grafana:
      "https://grafana.incidentflow.io/d/rYdddlPWk/node-exporter-full?viewPanel=78&var-job=kubernetes-service-endpoints"
  },
  warnings: []
};

describe("Grafana panel payload extraction", () => {
  it("extracts structuredContent from an MCP tool result envelope", () => {
    const envelope = {
      structuredContent: memoryBasicPanelView,
      content: [{ type: "text", text: "Grafana panel Memory Basic loaded." }],
      _meta: { datasourceUid: "PBFA97CFB590B2093", rawPanelType: "timeseries" }
    };

    const payload = getPanelPayload(envelope);

    expect(grafanaPanelViewSchema.safeParse(payload).success).toBe(true);
    expect(payload).toMatchObject({ panel: { title: "Memory Basic" } });
  });

  it("ignores host payload errors when a later source contains the real panel view", () => {
    const hostMessage = {
      payload: "Invalid input",
      mcpToolResult: {
        structuredContent: memoryBasicPanelView
      }
    };

    const selected = selectPanelPayload(["Invalid input", undefined, hostMessage]);

    expect(selected.panelPayload).toMatchObject({ panel: { id: 78, title: "Memory Basic" } });
    expect(grafanaPanelViewSchema.safeParse(selected.panelPayload).success).toBe(true);
  });

  it("finds a nested structuredContent object even when the host wraps it deeply", () => {
    const payload = getPanelPayload({
      type: "openai:tool_output",
      body: {
        payload: "Invalid input",
        detail: {
          result: {
            structuredContent: JSON.stringify(memoryBasicPanelView)
          }
        }
      }
    });

    expect(payload).toMatchObject({ dashboard: { uid: "rYdddlPWk" } });
  });

  it("falls back to the full openai globals object when a top-level toolOutput is not renderable", () => {
    const selected = selectPanelPayload([
      "Invalid input",
      {
        toolOutput: "Invalid input",
        nested: {
          mcpToolResult: {
            structuredContent: memoryBasicPanelView
          }
        }
      }
    ]);

    expect(selected.panelPayload).toMatchObject({ panel: { title: "Memory Basic" } });
  });
});
