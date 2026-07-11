import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import type { GrafanaPanelView } from "../contracts/grafana-panel-view";
import type { SelectedInterval } from "./types";
import { formatTimestamp, formatValue } from "./formatters";
import { GrafanaTooltip } from "./GrafanaTooltip";

const COLORS = ["#2563eb", "#0891b2", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#475569"];

type ChartProps = {
  panelView: GrafanaPanelView;
  selectedInterval?: SelectedInterval;
  onSelectInterval: (interval: SelectedInterval) => void;
};

export function GrafanaTimeSeriesChart({
  panelView,
  selectedInterval,
  onSelectInterval
}: ChartProps) {
  const Chart = panelView.visualization.type === "area" ? AreaChart : LineChart;
  const hasData = panelView.data.length > 0 && panelView.series.length > 0;

  if (!hasData) {
    return <div className="chart-empty">No data returned for this panel.</div>;
  }

  return (
    <div className="chart-shell">
      <ResponsiveContainer width="100%" height={320}>
        <Chart data={panelView.data} margin={{ top: 12, right: 20, bottom: 8, left: 2 }}>
          <CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" />
          <XAxis
            dataKey="timestamp"
            tickFormatter={formatTimestamp}
            tick={{ fontSize: 11, fill: "#526173" }}
            minTickGap={28}
            type="number"
            domain={["dataMin", "dataMax"]}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "#526173" }}
            tickFormatter={(value) => formatValue(Number(value), panelView.panel.unit)}
            width={58}
          />
          {panelView.visualization.showTooltip ? (
            <Tooltip content={<GrafanaTooltip panelView={panelView} />} />
          ) : null}
          {panelView.visualization.showLegend ? (
            <Legend wrapperStyle={{ fontSize: 12, paddingTop: 8 }} />
          ) : null}
          {selectedInterval ? (
            <ReferenceArea
              x1={selectedInterval.from}
              x2={selectedInterval.to}
              fill="#2563eb"
              fillOpacity={0.08}
              strokeOpacity={0}
            />
          ) : null}
          {panelView.annotations
            .filter((annotation) => annotation.type === "spike" && annotation.timestamp)
            .map((annotation) => (
              <ReferenceLine
                key={annotation.id}
                x={annotation.timestamp}
                stroke="#dc2626"
                strokeDasharray="4 4"
                label={{ value: annotation.label, fontSize: 11, fill: "#991b1b" }}
                onClick={() => {
                  const timestamp = annotation.timestamp ?? panelView.timeRange.from;
                  onSelectInterval({
                    from: timestamp - 5 * 60 * 1000,
                    to: timestamp + 5 * 60 * 1000,
                    annotationId: annotation.id
                  });
                }}
              />
            ))}
          {panelView.series.map((series, index) =>
            panelView.visualization.type === "area" ? (
              <Area
                key={series.key}
                type="monotone"
                dataKey={series.key}
                name={series.name}
                stroke={series.color ?? COLORS[index % COLORS.length]}
                fill={series.color ?? COLORS[index % COLORS.length]}
                fillOpacity={panelView.visualization.stacked ? 0.45 : 0.16}
                stackId={panelView.visualization.stacked ? "stack" : undefined}
                connectNulls
                dot={false}
                strokeWidth={2}
              />
            ) : (
              <Line
                key={series.key}
                type="monotone"
                dataKey={series.key}
                name={series.name}
                stroke={series.color ?? COLORS[index % COLORS.length]}
                connectNulls
                dot={false}
                strokeWidth={2}
              />
            )
          )}
        </Chart>
      </ResponsiveContainer>
    </div>
  );
}
