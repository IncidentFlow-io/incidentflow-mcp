import type { GrafanaPanelView } from "../contracts/grafana-panel-view";
import { GrafanaTimeSeriesChart } from "./GrafanaTimeSeriesChart";
import { PanelHeader } from "./PanelHeader";
import { formatTimeRange } from "./formatters";
import { useWidgetState } from "./useWidgetState";

type GrafanaPanelCardProps = {
  panelView: GrafanaPanelView;
};

export function GrafanaPanelCard({ panelView }: GrafanaPanelCardProps) {
  const [widgetState, setWidgetState] = useWidgetState();
  const selectedInterval = widgetState.selectedInterval;

  return (
    <main className="panel-card">
      <PanelHeader panelView={panelView} />
      <GrafanaTimeSeriesChart
        panelView={panelView}
        selectedInterval={selectedInterval}
        onSelectInterval={(interval) => setWidgetState({ selectedInterval: interval })}
      />
      <footer className="panel-footer">
        <div className="footer-copy">
          {selectedInterval ? formatTimeRange(selectedInterval.from, selectedInterval.to) : "Full range"}
        </div>
      </footer>
      {panelView.warnings.length > 0 ? (
        <ul className="warning-list">
          {panelView.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </main>
  );
}
