import { useEffect } from "react";
import { grafanaPanelViewSchema } from "./contracts/grafana-panel-view";
import { GrafanaPanel } from "./GrafanaPanel";
import { PanelEmptyState } from "./grafana-panel/PanelEmptyState";
import { useOpenAiGlobal } from "./grafana-panel/useOpenAiGlobal";

type ToolOutputEnvelope = {
  structuredContent?: unknown;
};

function getPanelPayload(toolOutput: unknown): unknown {
  if (toolOutput && typeof toolOutput === "object" && "structuredContent" in toolOutput) {
    return (toolOutput as ToolOutputEnvelope).structuredContent;
  }

  return toolOutput;
}

export function App() {
  const toolOutput = useOpenAiGlobal<unknown>("toolOutput");
  const input = useOpenAiGlobal<unknown>("input");
  const panelPayload = getPanelPayload(toolOutput);
  const parsed = grafanaPanelViewSchema.safeParse(panelPayload);

  useEffect(() => {
    if (!import.meta.env.DEV) {
      return;
    }

    console.log("Widget props", {
      toolOutput,
      structuredContent:
        toolOutput && typeof toolOutput === "object" && "structuredContent" in toolOutput
          ? (toolOutput as ToolOutputEnvelope).structuredContent
          : undefined,
      input
    });
  }, [input, toolOutput]);

  useEffect(() => {
    if (parsed.success) {
      return;
    }

    console.error("Grafana panel payload validation failed", {
      issues: parsed.error.issues,
      payload: panelPayload,
      rawToolOutput: toolOutput,
      input
    });
  }, [input, panelPayload, parsed, toolOutput]);

  if (!toolOutput) {
    return <PanelEmptyState title="Loading Grafana panel..." message="Preparing visualization." />;
  }

  if (!parsed.success) {
    return <PanelEmptyState title="Panel data is invalid" message="This panel cannot be rendered yet." />;
  }

  return <GrafanaPanel panelView={parsed.data} />;
}
