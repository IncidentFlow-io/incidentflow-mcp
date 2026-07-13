import type { GrafanaPanelView } from "./contracts/grafana-panel-view";
import { GrafanaPanelCard } from "./grafana-panel/GrafanaPanelCard";

type GrafanaPanelProps = {
  panelView: GrafanaPanelView;
};

export function GrafanaPanel({ panelView }: GrafanaPanelProps) {
  return <GrafanaPanelCard panelView={panelView} />;
}
