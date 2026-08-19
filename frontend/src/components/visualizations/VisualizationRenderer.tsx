import { useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, BarChart3, Maximize2, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { CandlestickChart, isIntradayTimeframe } from "@/components/charts/CandlestickChart";
import { StrategyConfirmationCard } from "@/components/visualizations/StrategyConfirmationCard";
import { BacktestResultCard } from "@/components/visualizations/BacktestResultCard";
import { api, type RunVisualization, type SimilarityRunVisualization } from "@/lib/api";
import { getChartTheme } from "@/lib/chart-theme";
import { abbreviateNum } from "@/lib/formatters";
import { cn } from "@/lib/utils";
import type {
  CandlestickVisualizationSpec,
  SimilarityRankingVisualizationSpec,
  VisualizationSpec,
} from "@/types/agent";

interface RendererProps {
  runId?: string;
  visualizations?: VisualizationSpec[];
}

interface ChartPanelProps {
  runId: string;
  spec: CandlestickVisualizationSpec;
}

interface VisualizationCacheEntry {
  data?: RunVisualization;
  promise?: Promise<RunVisualization>;
  controller?: AbortController;
  subscribers: number;
}

const MAX_VISUALIZATION_CACHE_ENTRIES = 100;
const visualizationCache = new Map<string, VisualizationCacheEntry>();

function visualizationCacheKey(runId: string, visualizationId: string): string {
  return `${runId}\u0000${visualizationId}`;
}

function pruneVisualizationCache(): void {
  if (visualizationCache.size <= MAX_VISUALIZATION_CACHE_ENTRIES) return;
  for (const [key, entry] of visualizationCache) {
    if (!entry.promise) visualizationCache.delete(key);
    if (visualizationCache.size <= MAX_VISUALIZATION_CACHE_ENTRIES) break;
  }
}

function acquireVisualization(
  runId: string,
  visualizationId: string,
): { promise: Promise<RunVisualization>; release: () => void } {
  const key = visualizationCacheKey(runId, visualizationId);
  let entry = visualizationCache.get(key);
  if (!entry) {
    const controller = new AbortController();
    entry = { controller, subscribers: 0 };
    const activeEntry = entry;
    activeEntry.promise = api.getRunVisualization(runId, visualizationId, controller.signal)
      .then((data) => {
        if (visualizationCache.get(key) === activeEntry) {
          activeEntry.data = data;
          activeEntry.promise = undefined;
          activeEntry.controller = undefined;
          visualizationCache.delete(key);
          visualizationCache.set(key, activeEntry);
          pruneVisualizationCache();
        }
        return data;
      })
      .catch((error: unknown) => {
        if (visualizationCache.get(key) === activeEntry) visualizationCache.delete(key);
        throw error;
      });
    visualizationCache.set(key, activeEntry);
  } else if (entry.data) {
    visualizationCache.delete(key);
    visualizationCache.set(key, entry);
  }

  const acquiredEntry = entry;
  acquiredEntry.subscribers += 1;
  let released = false;
  return {
    promise: acquiredEntry.data ? Promise.resolve(acquiredEntry.data) : acquiredEntry.promise!,
    release: () => {
      if (released) return;
      released = true;
      acquiredEntry.subscribers = Math.max(0, acquiredEntry.subscribers - 1);
      if (acquiredEntry.subscribers === 0 && acquiredEntry.promise) {
        acquiredEntry.controller?.abort();
        if (visualizationCache.get(key) === acquiredEntry) visualizationCache.delete(key);
      }
    },
  };
}

function invalidateVisualization(runId: string, visualizationId: string): void {
  const key = visualizationCacheKey(runId, visualizationId);
  const entry = visualizationCache.get(key);
  entry?.controller?.abort();
  visualizationCache.delete(key);
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof Error && reason.name === "AbortError";
}

function metadataLabel(spec: CandlestickVisualizationSpec): string {
  const provider = spec.provider_version
    ? `${spec.provider || "provider"}@${spec.provider_version}`
    : spec.provider;
  const canonical = spec.canonical_version
    ? `canonical ${spec.canonical_version.slice(0, 12)}`
    : undefined;
  const watermark = spec.watermark ? `through ${spec.watermark}` : undefined;
  const volumeUnitRaw = spec.units?.volume ?? spec.units?.vol;
  const volumeUnit = typeof volumeUnitRaw === "string"
    ? volumeUnitRaw
    : volumeUnitRaw && typeof volumeUnitRaw === "object" && "value" in volumeUnitRaw
      ? String(volumeUnitRaw.value)
      : undefined;
  return [
    spec.source,
    provider,
    canonical,
    spec.adjustment,
    spec.timeframe,
    spec.timezone,
    watermark,
    volumeUnit ? `volume ${volumeUnit}` : undefined,
    spec.completeness,
  ].filter(Boolean).join(" · ");
}

function humanizeStatus(value?: string): string | undefined {
  return value?.split("_").join(" ");
}

function formatPrice(value: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function ChartPanel({ runId, spec }: ChartPanelProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const [data, setData] = useState<RunVisualization | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [shouldLoad, setShouldLoad] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const chartData = data?.type === "candlestick_volume" ? data : null;

  const load = useCallback(() => {
    invalidateVisualization(runId, spec.data_ref);
    setData(null);
    setLoading(true);
    setError(null);
    setShouldLoad(true);
    setReloadVersion((version) => version + 1);
  }, [runId, spec.data_ref]);

  useEffect(() => {
    const element = panelRef.current;
    if (!element || typeof IntersectionObserver === "undefined") {
      setShouldLoad(true);
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      setShouldLoad(true);
      observer.disconnect();
    }, { rootMargin: "400px 0px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [runId, spec.data_ref]);

  useEffect(() => {
    if (!shouldLoad) return undefined;
    let active = true;
    setLoading(true);
    setError(null);
    const request = acquireVisualization(runId, spec.data_ref);
    request.promise
      .then((result) => { if (active) setData(result); })
      .catch((reason: unknown) => {
        if (active && !isAbortError(reason)) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => {
      active = false;
      request.release();
    };
  }, [reloadVersion, runId, shouldLoad, spec.data_ref]);

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
        {spec.fallback && (
          <div className="mt-1 text-[10px] text-amber-600 dark:text-amber-400" role="status">
            Fallback source used{spec.fallback_reason ? `: ${humanizeStatus(spec.fallback_reason)}` : ""}
          </div>
        )}
        {spec.warnings && spec.warnings.length > 0 && (
          <div className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">
            Data warning: {spec.warnings.join("; ")}
          </div>
        )}
        {chartData?.bars.length ? (() => {
          const latest = chartData.bars[chartData.bars.length - 1];
          const previous = chartData.bars.length > 1 ? chartData.bars[chartData.bars.length - 2] : undefined;
          const changePct = previous?.close
            ? ((latest.close - previous.close) / previous.close) * 100
            : undefined;
          const chartTheme = getChartTheme();
          return (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[10px] font-mono text-muted-foreground">
              <span>O {formatPrice(latest.open)}</span>
              <span>H {formatPrice(latest.high)}</span>
              <span>L {formatPrice(latest.low)}</span>
              <span className="font-semibold text-foreground">C {formatPrice(latest.close)}</span>
              {changePct != null && (
                <span style={{ color: changePct >= 0 ? chartTheme.upColor : chartTheme.downColor }}>
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
          onClick={() => { setShouldLoad(true); setExpanded(true); }}
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
    if (error || !chartData) {
      return (
        <div className="h-48 flex flex-col items-center justify-center gap-2 px-4 text-center">
          <p className="text-xs text-muted-foreground">
            {error
              || (data ? "Visualization payload type did not match its manifest." : null)
              || spec.fallback_text
              || "Chart unavailable"}
          </p>
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
    const chartTimeframe = chartData.timeframe || spec.timeframe;
    return (
      <div className="px-2 pt-2">
        <CandlestickChart
          data={chartData.bars}
          height={height}
          timeframe={chartTimeframe}
          linkGroup={false}
          initialRange={isIntradayTimeframe(chartTimeframe, chartData.bars) ? "5D" : "1Y"}
        />
      </div>
    );
  };

  return (
    <>
      <section ref={panelRef} className="not-prose overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm">
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

function scoreLabel(value: number | null): string {
  return value == null ? "Unavailable" : `${(value * 100).toFixed(1)}%`;
}

function similarityPayloadMatchesSpec(
  payload: SimilarityRunVisualization,
  spec: SimilarityRankingVisualizationSpec,
): boolean {
  return payload.visualization_id === spec.visualization_id
    && payload.similarity_run_id === spec.similarity_run_id
    && payload.similarity_sha256 === spec.similarity_sha256
    && payload.as_of === spec.as_of
    && payload.candidate_universe === spec.candidate_universe
    && payload.candidates.length === spec.candidate_count
    && Math.abs(payload.weights.business - spec.weights.business) <= 1e-12
    && Math.abs(payload.weights.factor - spec.weights.factor) <= 1e-12
    && Math.abs(payload.weights.price_volume - spec.weights.price_volume) <= 1e-12
    && payload.target_symbols.join("\u0000") === spec.target_symbols.join("\u0000")
    && payload.candidates.every((candidate, index) => (
      candidate.rank === index + 1
      && Number.isFinite(candidate.combined_score)
      && candidate.combined_score >= 0
      && candidate.combined_score <= 1
      && Number.isFinite(candidate.coverage)
      && candidate.coverage >= 0
      && candidate.coverage <= 1
      && candidate.evidence.length > 0
      && candidate.counterevidence.length > 0
    ));
}

function SimilarityPanel({
  runId,
  spec,
}: {
  runId: string;
  spec: SimilarityRankingVisualizationSpec;
}) {
  const { t } = useTranslation();
  const [data, setData] = useState<SimilarityRunVisualization | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadVersion, setReloadVersion] = useState(0);

  const load = useCallback(() => {
    invalidateVisualization(runId, spec.data_ref);
    setData(null);
    setError(null);
    setLoading(true);
    setReloadVersion((version) => version + 1);
  }, [runId, spec.data_ref]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    const request = acquireVisualization(runId, spec.data_ref);
    request.promise
      .then((result) => {
        if (!active) return;
        if (result.type !== "similarity_ranking" || !similarityPayloadMatchesSpec(result, spec)) {
          setError("Similarity payload did not match its content-bound manifest.");
          return;
        }
        setData(result);
      })
      .catch((reason: unknown) => {
        if (active && !isAbortError(reason)) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => {
      active = false;
      request.release();
    };
  }, [reloadVersion, runId, spec]);

  return (
    <section
      className="not-prose overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm"
      aria-label="Similarity ranking"
    >
      <header className="border-b border-border/60 px-3 py-3">
        <div className="flex items-start gap-2">
          <div className="mt-0.5 rounded-md bg-primary/10 p-1.5 text-primary">
            <BarChart3 className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold">{spec.title || "Similarity candidates"}</h3>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              Targets {spec.target_symbols.join(", ")} · as of {spec.as_of} · {spec.candidate_universe}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span className="rounded-full bg-muted px-2 py-0.5">Business {(spec.weights.business * 100).toFixed(0)}%</span>
              <span className="rounded-full bg-muted px-2 py-0.5">Factor {(spec.weights.factor * 100).toFixed(0)}%</span>
              <span className="rounded-full bg-muted px-2 py-0.5">Price/volume {(spec.weights.price_volume * 100).toFixed(0)}%</span>
            </div>
          </div>
        </div>
      </header>

      {loading && (
        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
          <span className="mr-2 h-4 w-4 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
          {t("visualization.loading", { defaultValue: "Loading research data…" })}
        </div>
      )}

      {!loading && (error || !data) && (
        <div className="flex h-40 flex-col items-center justify-center gap-2 px-4 text-center">
          <p className="text-xs text-muted-foreground">{error || spec.fallback_text || "Similarity ranking unavailable"}</p>
          <button
            type="button"
            onClick={load}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted"
          >
            <RefreshCw className="h-3 w-3" />
            {t("visualization.retry", { defaultValue: "Retry" })}
          </button>
        </div>
      )}

      {!loading && data && (
        <div className="divide-y divide-border/60">
          {data.candidates.map((candidate) => {
            const unavailableChannels = [
              candidate.business_score == null ? "business" : null,
              candidate.factor_score == null ? "factor" : null,
              candidate.price_volume_score == null ? "price/volume" : null,
            ].filter(Boolean);
            return (
              <article key={candidate.symbol} className="px-3 py-3">
                <div className="flex items-start gap-3">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold text-primary">
                    {candidate.rank}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                      <h4 className="font-mono text-sm font-semibold">{candidate.symbol}</h4>
                      <div className="flex gap-2 text-[11px]">
                        <span className="font-semibold text-foreground">Combined {scoreLabel(candidate.combined_score)}</span>
                        <span className="text-muted-foreground">Coverage {scoreLabel(candidate.coverage)}</span>
                        <span className="text-muted-foreground">Stability {scoreLabel(candidate.rank_stability)}</span>
                      </div>
                    </div>
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className="h-full rounded-full bg-primary" style={{ width: `${candidate.combined_score * 100}%` }} />
                    </div>
                    <dl className="mt-2 grid grid-cols-3 gap-1 text-[10px]">
                      <div className="rounded-md bg-muted/50 px-2 py-1"><dt className="text-muted-foreground">Business</dt><dd>{scoreLabel(candidate.business_score)}</dd></div>
                      <div className="rounded-md bg-muted/50 px-2 py-1"><dt className="text-muted-foreground">Factor</dt><dd>{scoreLabel(candidate.factor_score)}</dd></div>
                      <div className="rounded-md bg-muted/50 px-2 py-1"><dt className="text-muted-foreground">Price/volume</dt><dd>{scoreLabel(candidate.price_volume_score)}</dd></div>
                    </dl>
                    {(candidate.coverage < 1 || unavailableChannels.length > 0) && (
                      <p className="mt-2 flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400">
                        <AlertTriangle className="h-3 w-3" />
                        Degraded coverage{unavailableChannels.length ? ` · unavailable: ${unavailableChannels.join(", ")}` : ""}
                      </p>
                    )}
                    <div className="mt-2 grid gap-2 md:grid-cols-2">
                      <div>
                        <p className="text-[10px] font-medium uppercase tracking-wide text-emerald-600 dark:text-emerald-400">Evidence</p>
                        <ul className="mt-0.5 space-y-0.5 text-[11px] text-muted-foreground">
                          {candidate.evidence.map((item) => <li key={item}>+ {item}</li>)}
                        </ul>
                      </div>
                      <div>
                        <p className="text-[10px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">Counterevidence</p>
                        <ul className="mt-0.5 space-y-0.5 text-[11px] text-muted-foreground">
                          {candidate.counterevidence.map((item) => <li key={item}>− {item}</li>)}
                        </ul>
                      </div>
                    </div>
                  </div>
                </div>
              </article>
            );
          })}
          <footer className="px-3 py-2 text-[10px] text-muted-foreground">
            {data.excluded_symbol_count} symbols excluded · result {data.similarity_sha256.slice(0, 12)}…
          </footer>
        </div>
      )}
    </section>
  );
}

export function VisualizationRenderer({ runId, visualizations = [] }: RendererProps) {
  const [activeIndex, setActiveIndex] = useState(0);
  const charts = visualizations.filter((item) => item.type === "candlestick_volume");
  const rankings = visualizations.filter((item) => item.type === "similarity_ranking");
  const strategies = visualizations.filter((item) => item.type === "strategy_confirmation");
  const backtests = visualizations.filter((item) => item.type === "backtest_result");
  if (!runId || (charts.length === 0 && rankings.length === 0 && strategies.length === 0 && backtests.length === 0)) return null;
  const safeIndex = Math.min(activeIndex, Math.max(0, charts.length - 1));

  return (
    <div className="mt-3 space-y-2">
      {charts.length > 1 && (
        <div className="not-prose flex gap-1 overflow-x-auto pb-0.5">
          {charts.map((spec, index) => (
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
      {charts.length > 0 && (
        <ChartPanel
          key={`${runId}:${charts[safeIndex].visualization_id}`}
          runId={runId}
          spec={charts[safeIndex]}
        />
      )}
      {rankings.map((spec) => (
        <SimilarityPanel
          key={`${runId}:${spec.visualization_id}`}
          runId={runId}
          spec={spec}
        />
      ))}
      {strategies.map((spec) => (
        <StrategyConfirmationCard
          key={`${runId}:${spec.visualization_id}`}
          runId={runId}
          spec={spec}
        />
      ))}
      {backtests.map((spec) => (
        <BacktestResultCard
          key={`${runId}:${spec.visualization_id}`}
          spec={spec}
        />
      ))}
    </div>
  );
}
