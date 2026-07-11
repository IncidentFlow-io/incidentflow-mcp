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

export function formatValue(value: number | null | undefined, unit?: string | null): string {
  if (value == null || Number.isNaN(value)) {
    return "n/a";
  }
  const formatted = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: Math.abs(value) >= 10 ? 1 : 3
  }).format(value);
  return unit ? `${formatted} ${unit}` : formatted;
}

export function formatVariableValue(value: string | string[]): string {
  return Array.isArray(value) ? value.join(", ") : value;
}
