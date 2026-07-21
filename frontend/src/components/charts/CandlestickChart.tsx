import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import i18n from "@/i18n";
import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import type { PriceBar, TradeMarker, IndicatorPoint } from "@/lib/api";
import { calcMA, calcBOLL, calcMACD, calcRSI, calcKDJ, calcEMA } from "@/lib/indicators";
import { getChartTheme } from "@/lib/chart-theme";
import { abbreviateNum } from "@/lib/formatters";
import { echarts, CHART_GROUP, connectCharts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";

type Sub = "vol" | "macd" | "rsi" | "kdj";
export type ChartRangePreset = "1D" | "5D" | "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y" | "ALL";
type Overlay = "ma5" | "ma10" | "ma20" | "ma60" | "ema12" | "ema26" | "boll";

const OVERLAY_OPTIONS: { id: Overlay; label: string; group: string }[] = [
  { id: "ma5", label: "MA5", group: "MA" },
  { id: "ma10", label: "MA10", group: "MA" },
  { id: "ma20", label: "MA20", group: "MA" },
  { id: "ma60", label: "MA60", group: "MA" },
  { id: "ema12", label: "EMA12", group: "MA" },
  { id: "ema26", label: "EMA26", group: "MA" },
  { id: "boll", label: "BOLL", group: "Channel" },
];

const DAILY_RANGE_OPTIONS: ChartRangePreset[] = ["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"];
const INTRADAY_RANGE_OPTIONS: ChartRangePreset[] = ["1D", "5D", "1M", "3M", "ALL"];
const RANGE_DAYS: Partial<Record<ChartRangePreset, number>> = {
  "1D": 1,
  "5D": 5,
  "1M": 31,
  "3M": 92,
  "6M": 183,
  "1Y": 365,
  "3Y": 1095,
  "5Y": 1825,
};
const OVERLAY_COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6", "#ec4899", "#10b981", "#f97316", "#6366f1"];

export function isIntradayTimeframe(timeframe: string | undefined, data: PriceBar[] = []): boolean {
  if (timeframe) return ["1m", "5m", "15m", "30m", "1H"].includes(timeframe);
  return data.some((bar) => bar.time.includes("T") || /\d{2}:\d{2}/.test(bar.time));
}

export function getRangeOptions(timeframe: string | undefined, data: PriceBar[] = []): ChartRangePreset[] {
  return isIntradayTimeframe(timeframe, data) ? INTRADAY_RANGE_OPTIONS : DAILY_RANGE_OPTIONS;
}

export function rangeStartPercent(data: PriceBar[], range: ChartRangePreset): number {
  if (range === "ALL" || data.length < 2) return 0;
  const days = RANGE_DAYS[range];
  if (!days) return 0;
  const lastTime = Date.parse(data[data.length - 1].time.replace(" ", "T"));
  if (!Number.isFinite(lastTime)) return 0;
  const threshold = lastTime - days * 86_400_000;
  const startIndex = data.findIndex((bar) => {
    const timestamp = Date.parse(bar.time.replace(" ", "T"));
    return Number.isFinite(timestamp) && timestamp >= threshold;
  });
  return startIndex <= 0 ? 0 : (startIndex / data.length) * 100;
}

export type ChartLinkGroup = string | false;

export function resolveChartLinkGroup(linkGroup: ChartLinkGroup | undefined): string | null {
  if (linkGroup === false) return null;
  return linkGroup || CHART_GROUP;
}

function escapeTooltipText(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function formatCandlestickTooltip(params: unknown): string {
  if (!Array.isArray(params) || params.length === 0) return "";
  const first = params[0] as Record<string, unknown>;
  const lines = [escapeTooltipText(first.axisValue)];

  for (const raw of params) {
    if (!raw || typeof raw !== "object") continue;
    const item = raw as Record<string, unknown>;
    const seriesName = escapeTooltipText(item.seriesName);
    if (item.seriesName === "K" && Array.isArray(item.value) && item.value.length >= 4) {
      const [open, close, low, high] = item.value.slice(0, 4).map(Number);
      if (![open, close, low, high].every(Number.isFinite)) continue;
      const change = close - open;
      const percent = open ? ((change / open) * 100).toFixed(2) : "0.00";
      const sign = change >= 0 ? "+" : "";
      lines.push(`O: ${open.toFixed(2)}  H: ${high.toFixed(2)}`);
      lines.push(`L: ${low.toFixed(2)}  C: ${close.toFixed(2)} ${sign}${change.toFixed(2)} (${sign}${percent}%)`);
    } else if (item.seriesName === "Vol") {
      const volume = Number(item.value);
      if (Number.isFinite(volume)) lines.push(`Vol: ${abbreviateNum(volume)}`);
    } else if (item.value != null) {
      const value = Number(item.value);
      if (Number.isFinite(value)) lines.push(`${seriesName}: ${value.toFixed(2)}`);
    }
  }
  return lines.join("\n");
}

export function formatMarkerTooltip(param: unknown): string {
  if (!param || typeof param !== "object") return "";
  const item = param as Record<string, unknown>;
  return escapeTooltipText(item.name ?? item.value);
}

interface Props {
  data: PriceBar[];
  markers?: TradeMarker[];
  indicators?: Record<string, IndicatorPoint[]>;
  height?: number;
  timeframe?: string;
  linkGroup?: ChartLinkGroup;
}

export function CandlestickChart({ data, markers, indicators, height = 500, timeframe, linkGroup }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);
  const userZoomRef = useRef<{ start: number; end: number } | null>(null);
  const intraday = isIntradayTimeframe(timeframe, data);
  const resolvedLinkGroup = resolveChartLinkGroup(linkGroup);
  const [sub, setSub] = useState<Sub>("vol");
  const [range, setRange] = useState<ChartRangePreset>(intraday ? "5D" : "1Y");
  const [overlays, setOverlays] = useState<Set<Overlay>>(new Set(["ma5", "ma20"]));
  const [showMenu, setShowMenu] = useState(false);
  const { dark } = useDarkMode();
  const rangeOptions = getRangeOptions(timeframe, data);

  useEffect(() => {
    userZoomRef.current = null;
    setRange(intraday ? "5D" : "1Y");
  }, [intraday]);

  useEffect(() => {
    userZoomRef.current = null;
  }, [data]);

  const toggleOverlay = useCallback((id: Overlay) => {
    setOverlays(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  // Memoize base data arrays — only recompute when raw data changes
  const baseData = useMemo(() => {
    const dates = data.map(d => d.time);
    const closes = data.map(d => d.close);
    const highs = data.map(d => d.high);
    const lows = data.map(d => d.low);
    const opens = data.map(d => d.open);
    const candle = data.map(d => [d.open, d.close, d.low, d.high]);
    return { dates, closes, highs, lows, opens, candle };
  }, [data]);

  // Memoize indicator calculations — only recompute when data changes (not on overlay toggle)
  const indicatorCache = useMemo(() => ({
    ma5: calcMA(baseData.closes, 5),
    ma10: calcMA(baseData.closes, 10),
    ma20: calcMA(baseData.closes, 20),
    ma60: calcMA(baseData.closes, 60),
    ema12: calcEMA(baseData.closes, 12),
    ema26: calcEMA(baseData.closes, 26),
    boll: calcBOLL(baseData.closes, 20, 2),
    macd: calcMACD(baseData.closes),
    rsi: calcRSI(baseData.closes),
    kdj: calcKDJ(baseData.highs, baseData.lows, baseData.closes),
  }), [baseData]);

  // Memoize backend indicator series with Map lookup (O(1) instead of O(n) find)
  const extraIndicators = useMemo(() => {
    if (!indicators) return [];
    return Object.entries(indicators).map(([name, points]) => {
      const lookup = new Map(points.map(p => [p.time, p.value]));
      return { name: name.toUpperCase(), values: baseData.dates.map(d => lookup.get(d) ?? null) };
    });
  }, [indicators, baseData.dates]);

  // Init chart instance — only on mount/unmount and dark mode change
  useEffect(() => {
    if (!containerRef.current || data.length === 0) return;
    const chart = echarts.init(containerRef.current);
    if (resolvedLinkGroup) {
      chart.group = resolvedLinkGroup;
      connectCharts(resolvedLinkGroup);
    }
    chartRef.current = chart;

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(containerRef.current);
    const onDataZoom = () => {
      const zoom = (chart.getOption().dataZoom as Array<{ start?: number; end?: number }> | undefined)?.[0];
      if (typeof zoom?.start === "number" && typeof zoom?.end === "number") {
        userZoomRef.current = { start: zoom.start, end: zoom.end };
      }
    };
    chart.on("dataZoom", onDataZoom);
    return () => {
      chart.off("dataZoom", onDataZoom);
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, [data.length === 0, dark, resolvedLinkGroup]); // only re-init when going empty↔non-empty, theme, or link group changes

  // Update chart options — setOption on existing instance, no dispose
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || data.length === 0) return;

    const t = getChartTheme();
    const { dates, closes, opens, candle } = baseData;

    // Overlay series
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const overlaySeries: any[] = [];
    const legendNames: string[] = ["K"];
    let colorIdx = 0;

    const overlayMap: Record<string, { name: string; data: (number | null)[] }> = {
      ma5: { name: "MA5", data: indicatorCache.ma5 },
      ma10: { name: "MA10", data: indicatorCache.ma10 },
      ma20: { name: "MA20", data: indicatorCache.ma20 },
      ma60: { name: "MA60", data: indicatorCache.ma60 },
      ema12: { name: "EMA12", data: indicatorCache.ema12 },
      ema26: { name: "EMA26", data: indicatorCache.ema26 },
    };

    for (const [key, { name, data: lineData }] of Object.entries(overlayMap)) {
      if (overlays.has(key as Overlay)) {
        overlaySeries.push({ name, type: "line", data: lineData, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: OVERLAY_COLORS[colorIdx], width: 1 } });
        legendNames.push(name);
        colorIdx++;
      }
    }

    if (overlays.has("boll")) {
      const boll = indicatorCache.boll;
      overlaySeries.push(
        { name: "BOLL+", type: "line", data: boll.upper, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
        { name: "BOLL", type: "line", data: boll.mid, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 1 } },
        { name: "BOLL-", type: "line", data: boll.lower, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
      );
      legendNames.push("BOLL");
    }

    // Trade markers
    const marks = (markers || []).map(m => ({
      coord: [m.time, m.price],
      value: m.side === "BUY" ? "B" : "S",
      name: [`${m.side} @ ${m.price}`, m.qty ? `Qty: ${m.qty}` : "", m.reason || ""].filter(Boolean).join("\n"),
      itemStyle: { color: m.side === "BUY" ? t.upColor : t.downColor },
      label: { color: "#fff", fontSize: 10, fontWeight: "bold" as const },
    }));

    // Volume
    const vol = data.map((d, i) => ({
      value: d.volume,
      itemStyle: { color: closes[i] >= opens[i] ? t.volumeUp : t.volumeDown },
    }));

    // Sub-chart
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let subSeries: any[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let subYAxis: any = { scale: true, gridIndex: 1, splitLine: { lineStyle: { color: t.gridColor } }, axisLabel: { color: t.textColor, fontSize: 10 } };

    if (sub === "vol") {
      subSeries = [{ name: "Vol", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1 }];
      subYAxis = { ...subYAxis, axisLabel: { ...subYAxis.axisLabel, formatter: (v: number) => abbreviateNum(v) } };
      legendNames.push("Vol");
    } else if (sub === "macd") {
      const m = indicatorCache.macd;
      subSeries = [
        { name: "DIF", type: "line", data: m.dif, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
        { name: "DEA", type: "line", data: m.signal, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
        { name: "MACD", type: "bar", data: m.histogram.map(v => ({ value: v ?? 0, itemStyle: { color: (v ?? 0) >= 0 ? t.upColor : t.downColor } })), xAxisIndex: 1, yAxisIndex: 1 },
      ];
      legendNames.push("DIF", "DEA", "MACD");
    } else if (sub === "rsi") {
      subSeries = [{ name: "RSI", type: "line", data: indicatorCache.rsi, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1.5, color: t.infoColor } }];
      subYAxis = { ...subYAxis, min: 0, max: 100 };
      legendNames.push("RSI");
    } else {
      const kdj = indicatorCache.kdj;
      subSeries = [
        { name: "%K", type: "line", data: kdj.k, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
        { name: "%D", type: "line", data: kdj.d, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
        { name: "%J", type: "line", data: kdj.j, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: "#a855f7" } },
      ];
      legendNames.push("%K", "%D", "%J");
    }

    // Backend custom indicators (Map-based O(1) lookup)
    const extraSeries = extraIndicators.map((ind, i) => {
      legendNames.push(ind.name);
      return { name: ind.name, type: "line" as const, data: ind.values, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: OVERLAY_COLORS[(colorIdx + i) % OVERLAY_COLORS.length], type: "dashed" as const } };
    });

    const selectedZoom = userZoomRef.current;
    const defaultStart = selectedZoom?.start ?? rangeStartPercent(data, range);
    const defaultEnd = selectedZoom?.end ?? 100;

    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross" },
        renderMode: "richText",
        backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 11 },
        formatter: formatCandlestickTooltip,
      },
      toolbox: {
        feature: { saveAsImage: { title: "Save" }, dataZoom: { title: { zoom: "Zoom", back: "Reset" } }, restore: { title: "Reset" } },
        right: 8, top: 0, iconStyle: { borderColor: t.textColor },
      },
      legend: { data: legendNames, textStyle: { color: t.textColor, fontSize: 10 }, right: 80, top: 2, type: "scroll", itemWidth: 12, itemHeight: 8, itemGap: 8 },
      grid: [
        { left: 8, right: 8, top: 36, height: "55%", containLabel: true },
        { left: 8, right: 8, top: "66%", height: "22%", containLabel: true },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLine: { lineStyle: { color: t.axisColor } }, axisLabel: { color: t.textColor, fontSize: 10 }, boundaryGap: true },
        { type: "category", data: dates, gridIndex: 1, axisLine: { lineStyle: { color: t.axisColor } }, axisLabel: { show: false }, boundaryGap: true },
      ],
      yAxis: [
        { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: t.gridColor } }, axisLabel: { color: t.textColor, fontSize: 10 } },
        subYAxis,
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: defaultStart, end: defaultEnd },
        { type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 20, labelFormatter: (val: string) => val },
      ],
      series: [
        {
          name: "K", type: "candlestick", data: candle, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: t.upColor, color0: t.downColor, borderColor: t.upColor, borderColor0: t.downColor },
          markPoint: marks.length > 0 ? {
            data: marks,
            symbolSize: 28,
            tooltip: { renderMode: "richText", formatter: formatMarkerTooltip },
          } : undefined,
        },
        ...overlaySeries,
        ...extraSeries,
        ...subSeries,
      ],
    }, true);
  }, [data, markers, baseData, indicatorCache, extraIndicators, sub, range, overlays, dark]);

  if (data.length === 0) {
    return <div className="text-muted-foreground text-sm p-4">{i18n.t("charts.noPriceData")}</div>;
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-1 flex-wrap">
        {/* Time range */}
        <div className="flex gap-0.5">
          {rangeOptions.map((r) => (
            <button key={r} onClick={() => { userZoomRef.current = null; setRange(r); }} className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono transition-colors", range === r ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")}>{r}</button>
          ))}
        </div>

        <div className="w-px h-3 bg-border/40" />

        {/* Indicator dropdown */}
        <div className="relative">
          <button
            onClick={() => setShowMenu(!showMenu)}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
          >
            Indicators ({overlays.size}) <ChevronDown className="h-3 w-3" />
          </button>
          {showMenu && (
            <div className="absolute top-full left-0 mt-1 z-50 bg-card border rounded-lg shadow-lg p-2 min-w-[160px]" onMouseLeave={() => setShowMenu(false)}>
              {["MA", "Channel"].map(group => (
                <div key={group}>
                  <p className="text-[9px] text-muted-foreground/50 uppercase tracking-wider px-1 pt-1">{group}</p>
                  {OVERLAY_OPTIONS.filter(o => o.group === group).map(o => (
                    <label key={o.id} className="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-muted/30 cursor-pointer">
                      <input type="checkbox" checked={overlays.has(o.id)} onChange={() => toggleOverlay(o.id)} className="h-3 w-3 rounded accent-primary" />
                      <span className="text-xs">{o.label}</span>
                    </label>
                  ))}
                </div>
              ))}
              <div className="border-t mt-1 pt-1">
                <button onClick={() => { setOverlays(new Set()); setShowMenu(false); }} className="text-[10px] text-muted-foreground hover:text-foreground px-1 py-0.5 w-full text-left rounded hover:bg-muted/30">
                  Bare K (clear all)
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="w-px h-3 bg-border/40" />

        {/* Sub-chart selector */}
        <div className="flex gap-0.5">
          {(["vol", "macd", "rsi", "kdj"] as const).map((id) => (
            <button key={id} onClick={() => setSub(id)} className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono uppercase transition-colors", sub === id ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")}>{id}</button>
          ))}
        </div>
      </div>
      <div ref={containerRef} style={{ height }} />
    </div>
  );
}
