import type { GrafanaPanelView } from "../contracts/grafana-panel-view";
import { formatTimeRange, formatVariableValue } from "./formatters";

type PanelHeaderProps = {
  panelView: GrafanaPanelView;
};

export function PanelHeader({ panelView }: PanelHeaderProps) {
  const variables = Object.entries(panelView.variables);

  return (
    <header className="panel-header">
      <div>
        <p className="dashboard-title">{panelView.dashboard.title}</p>
        <h1>{panelView.panel.title}</h1>
        <p className="time-range">
          {formatTimeRange(panelView.timeRange.from, panelView.timeRange.to)}
        </p>
      </div>
      <a className="grafana-link" href={panelView.links.grafana} target="_blank" rel="noreferrer">
        Open in Grafana
      </a>
      {variables.length > 0 ? (
        <div className="variable-row" aria-label="Grafana variables">
          {variables.map(([name, value]) => (
            <span className="variable-pill" key={name}>
              <strong>{name}</strong>
              {formatVariableValue(value)}
            </span>
          ))}
        </div>
      ) : null}
    </header>
  );
}
