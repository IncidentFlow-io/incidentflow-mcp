import type { GrafanaPanelView } from "../contracts/grafana-panel-view";
import { formatTimestamp, formatValue } from "./formatters";

type TooltipPayloadItem = {
  dataKey?: string | number;
  name?: string | number;
  value?: number | string;
  color?: string;
};

type GrafanaTooltipProps = {
  active?: boolean;
  label?: string | number;
  payload?: TooltipPayloadItem[];
  panelView: GrafanaPanelView;
};

export function GrafanaTooltip({ active, label, payload, panelView }: GrafanaTooltipProps) {
  if (!active || !payload?.length || typeof label !== "number") {
    return null;
  }

  return (
    <div className="chart-tooltip">
      <p className="tooltip-time">{formatTimestamp(label)}</p>
      {payload.map((item) => {
        const series = panelView.series.find((entry) => entry.key === item.dataKey);
        return (
          <div className="tooltip-row" key={String(item.dataKey)}>
            <span className="tooltip-swatch" style={{ backgroundColor: item.color }} />
            <span>{series?.name ?? item.name}</span>
            <strong>{formatValue(Number(item.value), series?.unit)}</strong>
          </div>
        );
      })}
    </div>
  );
}
