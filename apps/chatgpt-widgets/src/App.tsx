import { grafanaPanelViewSchema } from "./contracts/grafana-panel-view";
import { GrafanaPanel } from "./GrafanaPanel";
import { PanelEmptyState } from "./grafana-panel/PanelEmptyState";
import { useOpenAiGlobal } from "./grafana-panel/useOpenAiGlobal";

export function App() {
  const toolOutput = useOpenAiGlobal<unknown>("toolOutput");
  const parsed = grafanaPanelViewSchema.safeParse(toolOutput);

  if (!toolOutput) {
    return <PanelEmptyState title="Loading Grafana panel..." message="Preparing visualization." />;
  }

  if (!parsed.success) {
    return <PanelEmptyState title="Panel data is invalid" message="This panel cannot be rendered yet." />;
  }

  return <GrafanaPanel panelView={parsed.data} />;
}
