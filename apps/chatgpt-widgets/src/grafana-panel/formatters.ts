export function formatTimeRange(from: number, to: number): string {
  return `${formatTimestamp(from)} - ${formatTimestamp(to)}`;
}

export function formatTimestamp(value: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

// Grafana unit ids that mean "no unit": they select a number format and must
// never be rendered as a suffix (otherwise the chart shows e.g. "151.5 short").
const UNITLESS_GRAFANA_UNITS = new Set(["short", "none"]);

// Grafana rate unit ids -> human labels (ids like "reqpm" are not readable).
const GRAFANA_UNIT_LABELS: Readonly<Record<string, string>> = {
  reqps: "req/s",
  reqpm: "req/min",
  rps: "reads/s",
  wps: "writes/s",
  iops: "io/s",
  ops: "ops/s",
  opm: "ops/min"
};

// Grafana's "short" format: plain number with K/M/B/T abbreviation.
const SHORT_SCALE: ReadonlyArray<readonly [number, string]> = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"]
];

function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: Math.abs(value) >= 10 ? 1 : 3
  }).format(value);
}

function formatShort(value: number): string {
  const abs = Math.abs(value);
  for (const [threshold, suffix] of SHORT_SCALE) {
    if (abs >= threshold) {
      return `${formatNumber(value / threshold)}${suffix}`;
    }
  }
  return formatNumber(value);
}

export function formatValue(value: number | null | undefined, unit?: string | null): string {
  if (value == null || Number.isNaN(value)) {
    return "n/a";
  }
  const normalizedUnit = unit?.trim() ?? "";
  if (normalizedUnit === "short") {
    return formatShort(value);
  }
  const formatted = formatNumber(value);
  if (!normalizedUnit || UNITLESS_GRAFANA_UNITS.has(normalizedUnit)) {
    return formatted;
  }
  return `${formatted} ${GRAFANA_UNIT_LABELS[normalizedUnit] ?? normalizedUnit}`;
}

export function formatVariableValue(value: string | string[]): string {
  return Array.isArray(value) ? value.join(", ") : value;
}
