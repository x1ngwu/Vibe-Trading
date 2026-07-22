import { useCallback, useEffect, useId, useState } from "react";
import { Maximize2, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { api, type RunVisualization } from "@/lib/api";
import { abbreviateNum } from "@/lib/formatters";
import { cn } from "@/lib/utils";
import type { VisualizationSpec } from "@/types/agent";

interface RendererProps {
  runId?: string;
  visualizations?: VisualizationSpec[];
}

interface ChartPanelProps {
  runId: string;
  spec: VisualizationSpec;
}

function metadataLabel(spec: VisualizationSpec): string {
  return [spec.source, spec.adjustment, spec.timeframe, spec.timezone].filter(Boolean).join(" · ");
}

function formatPrice(value: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function ChartPanel({ runId, spec }: ChartPanelProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const [data, setData] = useState<RunVisualization | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getRunVisualization(runId, spec.data_ref));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [runId, spec.data_ref]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api.getRunVisualization(runId, spec.data_ref)
      .then((result) => { if (active) setData(result); })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [runId, spec.data_ref]);

  useEffect(() => {
    if (!expanded) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  const title = spec.title || `${spec.symbol || "K-line"}`;
  const subtitle = metadataLabel(spec);
  const range = spec.actual_start && spec.actual_end
    ? `${spec.actual_start} – ${spec.actual_end}`
    : undefined;

  const header = (expandedView = false) => (
    <div className="flex items-start justify-between gap-3 px-3 py-2 border-b border-border/60">
      <div className="min-w-0">
        <h3 id={expandedView ? titleId : undefined} className="text-sm font-semibold truncate">{title}</h3>
        <div className="flex flex-wrap gap-x-2 text-[10px] text-muted-foreground">
          {subtitle && <span>{subtitle}</span>}
          {range && <span>{range}</span>}
          {spec.bar_count != null && <span>{spec.truncated ? `latest ${spec.bar_count}` : spec.bar_count} bars</span>}
          {spec.dropped_bar_count != null && spec.dropped_bar_count > 0 && (
            <span>dropped {spec.dropped_bar_count} invalid/duplicate bars</span>
          )}
        </div>
        {data?.bars.length ? (() => {
          const latest = data.bars[data.bars.length - 1];
          const previous = data.bars.length > 1 ? data.bars[data.bars.length - 2] : undefined;
          const changePct = previous?.close
            ? ((latest.close - previous.close) / previous.close) * 100
            : undefined;
          return (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[10px] font-mono text-muted-foreground">
              <span>O {formatPrice(latest.open)}</span>
              <span>H {formatPrice(latest.high)}</span>
              <span>L {formatPrice(latest.low)}</span>
              <span className="font-semibold text-foreground">C {formatPrice(latest.close)}</span>
              {changePct != null && (
                <span className={changePct >= 0 ? "text-rose-500" : "text-emerald-500"}>
                  {changePct >= 0 ? "+" : ""}{changePct.toFixed(2)}%
                </span>
              )}
              <span>Vol {abbreviateNum(latest.volume)}</span>
            </div>
          );
        })() : null}
      </div>
      {expandedView ? (
        <button
          type="button"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={() => setExpanded(false)}
          aria-label={t("visualization.close", { defaultValue: "Close expanded chart" })}
        >
          <X className="h-4 w-4" />
        </button>
      ) : (
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded-md border border-border/70 px-2 py-1 text-[10px] text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={() => setExpanded(true)}
          aria-label={t("visualization.expand", { defaultValue: "Expand chart" })}
        >
          <Maximize2 className="h-3 w-3" />
          {t("visualization.expandShort", { defaultValue: "Expand" })}
        </button>
      )}
    </div>
  );

  const content = (height: number) => {
    if (loading) {
      return (
        <div className="h-[340px] flex items-center justify-center text-xs text-muted-foreground">
          <span className="h-4 w-4 mr-2 rounded-full border-2 border-primary/30 border-t-primary animate-spin" />
          {t("visualization.loading", { defaultValue: "Loading market data…" })}
        </div>
      );
    }
    if (error || !data) {
      return (
        <div className="h-48 flex flex-col items-center justify-center gap-2 px-4 text-center">
          <p className="text-xs text-muted-foreground">{error || spec.fallback_text || "Chart unavailable"}</p>
          <button
            type="button"
            onClick={load}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted"
          >
            <RefreshCw className="h-3 w-3" />
            {t("visualization.retry", { defaultValue: "Retry" })}
          </button>
        </div>
      );
    }
    return (
      <div className="px-2 pt-2">
        <CandlestickChart
          data={data.bars}
          height={height}
          timeframe={data.timeframe || spec.timeframe}
          linkGroup={false}
        />
      </div>
    );
  };

  return (
    <>
      <section className="not-prose overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm">
        {header()}
        {!expanded && content(340)}
      </section>
      {expanded && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/80 p-3 backdrop-blur-sm md:p-8"
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          onMouseDown={(event) => { if (event.target === event.currentTarget) setExpanded(false); }}
        >
          <section className="w-full max-w-7xl max-h-full overflow-auto rounded-xl border bg-card shadow-2xl">
            {header(true)}
            {content(Math.max(440, Math.min(680, window.innerHeight - 190)))}
          </section>
        </div>
      )}
    </>
  );
}

export function VisualizationRenderer({ runId, visualizations = [] }: RendererProps) {
  const [activeIndex, setActiveIndex] = useState(0);
  const supported = visualizations.filter((item) => item.type === "candlestick_volume");
  if (!runId || supported.length === 0) return null;
  const safeIndex = Math.min(activeIndex, supported.length - 1);

  return (
    <div className="mt-3 space-y-2">
      {supported.length > 1 && (
        <div className="not-prose flex gap-1 overflow-x-auto pb-0.5">
          {supported.map((spec, index) => (
            <button
              key={spec.visualization_id}
              type="button"
              onClick={() => setActiveIndex(index)}
              className={cn(
                "shrink-0 rounded-md px-2.5 py-1 text-xs transition-colors",
                safeIndex === index
                  ? "bg-primary/15 text-primary"
                  : "bg-muted/50 text-muted-foreground hover:text-foreground",
              )}
            >
              {spec.symbol || spec.title || `Chart ${index + 1}`}
            </button>
          ))}
        </div>
      )}
      <ChartPanel
        key={supported[safeIndex].visualization_id}
        runId={runId}
        spec={supported[safeIndex]}
      />
    </div>
  );
}
