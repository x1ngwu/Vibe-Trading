import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  GitCompareArrows,
  LoaderCircle,
  RefreshCw,
  Square,
  XCircle,
} from "lucide-react";
import { EquityChart } from "@/components/charts/EquityChart";
import {
  api,
  type BacktestComparisonPayload,
  type BacktestResultPayload,
} from "@/lib/api";
import type { BacktestResultVisualizationSpec } from "@/types/agent";

interface Props {
  spec: BacktestResultVisualizationSpec;
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function metricValue(metric: string, value: number | null): string {
  if (value == null) return "—";
  if (metric === "trade_count") return String(value);
  return percent(value);
}

function StatusIcon({ status }: { status: BacktestResultPayload["status"] }) {
  if (status === "completed") return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
  if (status === "failed" || status === "cancelled") return <XCircle className="h-4 w-4 text-destructive" />;
  return <LoaderCircle className="h-4 w-4 animate-spin text-primary" />;
}

export function BacktestResultCard({ spec }: Props) {
  const [data, setData] = useState<BacktestResultPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [comparison, setComparison] = useState<BacktestComparisonPayload | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const terminalRef = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await api.getBacktest(spec.stream_id, spec.job_id);
      terminalRef.current = TERMINAL.has(payload.status);
      setData(payload);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [spec.job_id, spec.stream_id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!data || TERMINAL.has(data.status)) return undefined;
    let active = true;
    let source: EventSource | null = null;
    let reconnectTimer: number | undefined;

    const connect = async () => {
      try {
        source = new EventSource(await api.backtestSseUrl(spec.stream_id, spec.job_id));
        source.addEventListener("backtest", (event) => {
          if (!active) return;
          const payload = JSON.parse((event as MessageEvent).data) as BacktestResultPayload;
          terminalRef.current = TERMINAL.has(payload.status);
          setData(payload);
          setError(null);
        });
        source.addEventListener("done", () => {
          terminalRef.current = true;
          source?.close();
        });
        source.onerror = () => {
          source?.close();
          if (active && !terminalRef.current) {
            reconnectTimer = window.setTimeout(() => { void connect(); }, 500);
          }
        };
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        reconnectTimer = window.setTimeout(() => { void connect(); }, 1_000);
      }
    };
    void connect();
    return () => {
      active = false;
      source?.close();
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
    };
  }, [data?.status, spec.job_id, spec.stream_id]);

  const cancel = async () => {
    setCancelling(true);
    setError(null);
    try {
      await api.cancelBacktest(spec.stream_id, spec.job_id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCancelling(false);
    }
  };

  const comparePrevious = async () => {
    setCompareError(null);
    try {
      const jobs = await api.listBacktests(spec.stream_id);
      const previous = jobs.find((job) => (
        job.status === "completed"
        && job.job_id !== spec.job_id
        && job.strategy_version_id !== spec.strategy_version_id
      ));
      if (!previous) {
        setCompareError("没有可比较的已完成旧版本结果。");
        return;
      }
      setComparison(await api.compareBacktests(spec.stream_id, previous.job_id, spec.job_id));
    } catch (reason) {
      setCompareError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <section
      className="not-prose overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm"
      aria-label={`Backtest result version ${spec.strategy_version_number}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 px-3 py-3">
        <div className="flex min-w-0 items-start gap-2">
          <div className="mt-0.5 rounded-md bg-primary/10 p-1.5">
            <StatusIcon status={data?.status || "queued"} />
          </div>
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{spec.title}</h3>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              策略 v{spec.strategy_version_number} · {data?.status || "loading"} · {spec.job_id.slice(-12)}
            </p>
          </div>
        </div>
        <div className="flex gap-1.5">
          {data && !TERMINAL.has(data.status) && (
            <button
              type="button"
              onClick={() => { void cancel(); }}
              disabled={cancelling}
              className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
              aria-label="Cancel backtest"
            >
              <Square className="h-3 w-3" />
              {cancelling ? "取消中…" : "取消"}
            </button>
          )}
          <button
            type="button"
            onClick={() => { void load(); }}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted"
            aria-label="Refresh backtest"
          >
            <RefreshCw className="h-3 w-3" />
            刷新
          </button>
        </div>
      </header>

      {loading && !data && (
        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
          <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
          正在恢复持久化回测状态…
        </div>
      )}

      {error && (
        <div role="alert" className="flex items-start gap-2 border-b px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {data?.metrics && (
        <>
          <dl className="grid grid-cols-2 gap-px bg-border/60 sm:grid-cols-5">
            {[
              ["总收益", percent(data.metrics.total_return)],
              ["年化收益", data.metrics.annualized_return == null ? "—" : percent(data.metrics.annualized_return)],
              ["最大回撤", percent(data.metrics.max_drawdown)],
              ["换手", percent(data.metrics.turnover)],
              ["成交数", String(data.metrics.trade_count)],
            ].map(([label, value]) => (
              <div key={label} className="bg-card px-3 py-2">
                <dt className="text-[10px] text-muted-foreground">{label}</dt>
                <dd className="mt-0.5 font-mono text-sm font-semibold">{value}</dd>
              </div>
            ))}
          </dl>
          <div className="px-2 py-3">
            <EquityChart data={data.equity} trades={data.trades} height={300} />
            {data.trades.length > 0 && (
              <p className="px-1 text-[10px] text-muted-foreground">
                权益图三角/图钉标记买卖成交；展示 {data.trades.length} 条
                {data.truncated_trades ? "（已按上限采样）" : ""}。
              </p>
            )}
          </div>
        </>
      )}

      {data && (data.status === "failed" || data.status === "cancelled") && (
        <div className="space-y-2 px-3 py-3">
          <p className="text-sm font-medium">
            {data.status === "cancelled" ? "回测已取消" : "回测失败"}
          </p>
          {data.diagnostics.map((item, index) => (
            <div key={`${item.code}:${index}`} className="rounded-md bg-muted/60 px-2 py-1.5 text-xs">
              <span className="font-mono font-medium">{item.code}</span>
              <span className="ml-2 text-muted-foreground">{item.message}</span>
            </div>
          ))}
        </div>
      )}

      {data?.status === "completed" && (
        <div className="border-t border-border/60 px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-[10px] text-muted-foreground">
              snapshot {data.snapshot_sha256?.slice(0, 12)}… · engine {data.engine_commit?.slice(0, 12)}…
            </div>
            <button
              type="button"
              onClick={() => { void comparePrevious(); }}
              className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted"
            >
              <GitCompareArrows className="h-3 w-3" />
              与旧版本比较
            </button>
          </div>
          {compareError && <p role="status" className="mt-2 text-xs text-muted-foreground">{compareError}</p>}
          {comparison && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[420px] text-left text-xs">
                <caption className="sr-only">Backtest version comparison</caption>
                <thead className="text-muted-foreground">
                  <tr><th className="py-1">指标</th><th>旧版本</th><th>当前版本</th><th>变化</th></tr>
                </thead>
                <tbody>
                  {comparison.deltas.map((item) => (
                    <tr key={item.metric} className="border-t border-border/60">
                      <th className="py-1.5 font-medium">{item.metric}</th>
                      <td>{metricValue(item.metric, item.left)}</td>
                      <td>{metricValue(item.metric, item.right)}</td>
                      <td>{metricValue(item.metric, item.delta)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
