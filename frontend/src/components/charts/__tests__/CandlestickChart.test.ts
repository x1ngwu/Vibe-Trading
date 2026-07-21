import type { PriceBar } from "@/lib/api";
import { CHART_GROUP } from "@/lib/echarts";
import {
  formatCandlestickTooltip,
  formatMarkerTooltip,
  getRangeOptions,
  isIntradayTimeframe,
  rangeStartPercent,
  resolveChartLinkGroup,
} from "../CandlestickChart";

function bar(time: string): PriceBar {
  return { time, open: 10, high: 12, low: 9, close: 11, volume: 100 };
}

describe("CandlestickChart range helpers", () => {
  it("uses elapsed time instead of assuming one bar per trading day", () => {
    const data = [
      bar("2025-07-20"),
      bar("2026-07-19"),
      bar("2026-07-20"),
      bar("2026-07-21"),
    ];
    expect(rangeStartPercent(data, "1Y")).toBe(25);
    expect(rangeStartPercent(data, "ALL")).toBe(0);
  });

  it("offers short windows for intraday data and multi-year windows for daily data", () => {
    const minuteData = [bar("2026-07-21T09:30:00"), bar("2026-07-21T09:35:00")];
    expect(isIntradayTimeframe("5m", minuteData)).toBe(true);
    expect(getRangeOptions("5m", minuteData)).toEqual(["1D", "5D", "1M", "3M", "ALL"]);
    expect(getRangeOptions("1D")).toEqual(["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"]);
  });

  it("renders tooltip data as escaped rich text without HTML fragments", () => {
    const attack = `<img src=x onerror="alert(1)">`;
    const tooltip = formatCandlestickTooltip([
      { axisValue: attack, seriesName: "K", value: [10, 11, 9, 12] },
      { axisValue: attack, seriesName: "Vol", value: 1_500 },
      { axisValue: attack, seriesName: attack, marker: attack, value: 12.345 },
    ]);

    expect(tooltip).toContain("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
    expect(tooltip).not.toContain("<img");
    expect(tooltip).not.toContain("<br");
    expect(tooltip).toContain("O: 10.00  H: 12.00");
    expect(tooltip).toContain("L: 9.00  C: 11.00 +1.00 (+10.00%)");
    expect(tooltip).toContain("Vol: 1,500");
    expect(tooltip).toContain("12.35");
    expect(formatMarkerTooltip({ name: attack })).not.toContain("<img");
  });

  it("keeps Run Detail linked by default and allows chat charts to opt out", () => {
    expect(resolveChartLinkGroup(undefined)).toBe(CHART_GROUP);
    expect(resolveChartLinkGroup("run-detail-custom")).toBe("run-detail-custom");
    expect(resolveChartLinkGroup(false)).toBeNull();
  });
});
