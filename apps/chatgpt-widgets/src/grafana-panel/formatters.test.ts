import { describe, expect, it } from "vitest";

import { formatValue } from "./formatters";

describe("formatValue", () => {
  it("never renders the Grafana 'short' unit id as text", () => {
    expect(formatValue(151.5, "short")).toBe(formatValue(151.5, null));
    expect(formatValue(151.5, "short")).not.toContain("short");
  });

  it("abbreviates large values for the 'short' unit like Grafana", () => {
    expect(formatValue(1500, "short")).toBe(`${formatValue(1.5, null)}K`);
    expect(formatValue(2_500_000, "short")).toBe(`${formatValue(2.5, null)}M`);
    expect(formatValue(-3_000_000_000, "short")).toBe(`${formatValue(-3, null)}B`);
    expect(formatValue(999, "short")).toBe(formatValue(999, null));
  });

  it("drops the 'none' unit id", () => {
    expect(formatValue(42, "none")).toBe(formatValue(42, null));
  });

  it("keeps real units as a suffix", () => {
    expect(formatValue(42, "bytes")).not.toContain("short");
    expect(formatValue(42, "bytes")).toBe(`${formatValue(42, null)} bytes`);
  });

  it("labels Grafana rate unit ids readably", () => {
    expect(formatValue(220.3, "reqpm")).toBe(`${formatValue(220.3, null)} req/min`);
    expect(formatValue(42, "reqps")).toBe(`${formatValue(42, null)} req/s`);
    expect(formatValue(3, "ops")).toBe(`${formatValue(3, null)} ops/s`);
  });

  it("handles missing values", () => {
    expect(formatValue(null, "short")).toBe("n/a");
    expect(formatValue(Number.NaN, "short")).toBe("n/a");
  });
});
