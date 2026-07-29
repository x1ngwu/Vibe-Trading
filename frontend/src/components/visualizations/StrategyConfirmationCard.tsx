import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Clock3, RefreshCw, ShieldCheck } from "lucide-react";
import {
  ApiError,
  api,
  type StrategyConfirmationRunVisualization,
} from "@/lib/api";
import type { StrategyConfirmationVisualizationSpec } from "@/types/agent";
import { cn } from "@/lib/utils";

interface Props {
  runId: string;
  spec: StrategyConfirmationVisualizationSpec;
}

function payloadMatchesSpec(
  payload: StrategyConfirmationRunVisualization,
  spec: StrategyConfirmationVisualizationSpec,
): boolean {
  const currentVersion = payload.lifecycle_state !== "superseded";
  return payload.visualization_id === spec.visualization_id
    && payload.stream_id === spec.stream_id
    && payload.version.version_id === spec.version_id
    && payload.version.version_number === spec.version_number
    && payload.version.parent_version_id === spec.parent_version_id
    && (!currentVersion || payload.head.version_id === spec.version_id)
    && (spec.confirmation_hash === null
      ? payload.card === null
      : payload.card?.confirmation_hash === spec.confirmation_hash)
    && (payload.card === null
      || payload.data_basis?.snapshot_ref.object_id === payload.card.strategy.data_snapshot_ref.object_id);
}

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function stateLabel(state: StrategyConfirmationRunVisualization["lifecycle_state"]): string {
  return {
    needs_clarification: "需要补充",
    awaiting_confirmation: "等待确认",
    confirmed: "已确认",
    expired: "已过期",
    superseded: "已被新版本替代",
  }[state];
}

function newIdempotencyKey(visualizationId: string): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `strategy-confirm:${visualizationId}:${suffix}`.slice(0, 128);
}

export function StrategyConfirmationCard({ runId, spec }: Props) {
  const [data, setData] = useState<StrategyConfirmationRunVisualization | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const idempotencyKey = useRef(newIdempotencyKey(spec.visualization_id));

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getRunVisualization(runId, spec.data_ref);
      if (result.type !== "strategy_confirmation" || !payloadMatchesSpec(result, spec)) {
        throw new Error("策略确认卡与消息中的版本标识不一致。");
      }
      setData(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [runId, spec]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (data?.lifecycle_state !== "awaiting_confirmation" || !data.card) return undefined;
    const remaining = new Date(data.card.expires_at).getTime() - Date.now();
    if (remaining <= 0) {
      setData((current) => current ? { ...current, lifecycle_state: "expired" } : current);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setData((current) => (
        current?.lifecycle_state === "awaiting_confirmation"
          ? { ...current, lifecycle_state: "expired" }
          : current
      ));
    }, Math.min(remaining, 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [data]);

  const confirm = useCallback(async () => {
    if (!data?.card || data.lifecycle_state !== "awaiting_confirmation") return;
    setConfirming(true);
    setError(null);
    try {
      const result = await api.confirmStrategy(
        spec.stream_id,
        runId,
        spec.visualization_id,
        {
          expected_head: data.head,
          confirmation_hash: data.card.confirmation_hash,
          idempotency_key: idempotencyKey.current,
        },
      );
      if (!payloadMatchesSpec(result, spec) || result.lifecycle_state !== "confirmed") {
        throw new Error("确认响应未绑定当前策略版本。");
      }
      setData(result);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 410) {
        setData((current) => current ? { ...current, lifecycle_state: "expired" } : current);
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setConfirming(false);
    }
  }, [data, runId, spec]);

  if (loading) {
    return (
      <section className="not-prose flex h-32 items-center justify-center rounded-xl border bg-card text-xs text-muted-foreground">
        <span className="mr-2 h-4 w-4 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
        正在加载策略确认卡…
      </section>
    );
  }
  if (!data) {
    return (
      <section className="not-prose flex min-h-40 flex-col items-center justify-center gap-2 rounded-xl border bg-card px-4 text-center">
        <p className="text-xs text-muted-foreground">{error || spec.fallback_text}</p>
        <button type="button" onClick={() => void load()} className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted">
          <RefreshCw className="h-3 w-3" />重试
        </button>
      </section>
    );
  }

  const strategy = data.card?.strategy ?? data.version.strategy;
  const disabled = data.lifecycle_state !== "awaiting_confirmation" || confirming;
  const badgeTone = data.lifecycle_state === "confirmed"
    ? "bg-emerald-500/10 text-emerald-600"
    : data.lifecycle_state === "awaiting_confirmation"
      ? "bg-amber-500/10 text-amber-600"
      : "bg-muted text-muted-foreground";

  return (
    <section className="not-prose overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm" aria-label="策略确认卡">
      <header className="border-b border-border/60 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-primary" />
              <h3 className="text-sm font-semibold">{spec.title}</h3>
              <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-medium", badgeTone)}>
                {stateLabel(data.lifecycle_state)}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              版本 v{data.version.version_number}
              {data.version.parent_version_id ? ` · 基于 v${data.version.version_number - 1}` : " · 初始版本"}
              {" · "}{data.version.version_id.slice(17, 29)}…
            </p>
          </div>
          {data.card && (
            <p className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <Clock3 className="h-3 w-3" />
              有效期至 {new Date(data.card.expires_at).toLocaleString()}
            </p>
          )}
        </div>
      </header>

      {data.lifecycle_state === "needs_clarification" && (
        <div className="space-y-2 px-4 py-4">
          <p className="text-xs font-medium">继续前需要明确：</p>
          {data.version.clarifications.map((item) => (
            <div key={`${item.path}:${item.code}`} className="rounded-lg bg-muted/50 px-3 py-2">
              <p className="text-xs">{item.question}</p>
              {item.options.length > 0 && <p className="mt-1 text-[10px] text-muted-foreground">可选：{item.options.join(" / ")}</p>}
            </div>
          ))}
        </div>
      )}

      {strategy && (
        <div className="grid gap-3 px-4 py-4 text-xs md:grid-cols-2">
          <div className="rounded-lg bg-muted/40 p-3">
            <h4 className="font-medium">股票池与数据口径</h4>
            <p className="mt-1 break-words text-muted-foreground">{strategy.universe_symbols.join(", ")}</p>
            {data.data_basis && (
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[11px]">
                <dt className="text-muted-foreground">区间</dt><dd>{data.data_basis.start_date} 至 {data.data_basis.end_date}</dd>
                <dt className="text-muted-foreground">as-of</dt><dd>{data.data_basis.as_of}</dd>
                <dt className="text-muted-foreground">频率/复权</dt><dd>{data.data_basis.frequency} / {data.data_basis.adjustment}</dd>
                <dt className="text-muted-foreground">来源</dt><dd>{Array.from(new Set(Object.values(data.data_basis.actual_sources))).join(", ")}</dd>
                <dt className="text-muted-foreground">快照</dt><dd className="break-all font-mono">{data.data_basis.snapshot_ref.content_sha256.slice(0, 16)}…</dd>
              </dl>
            )}
          </div>

          <div className="rounded-lg bg-muted/40 p-3">
            <h4 className="font-medium">选股、排序与持仓</h4>
            <ul className="mt-1 space-y-1 text-[11px] text-muted-foreground">
              {strategy.signals.map((signal, index) => (
                <li key={`${signal.field}:${index}`}>
                  信号 {signal.field} {signal.operator} {String(signal.value)} · lookback {signal.lookback_days}
                </li>
              ))}
              {strategy.ranking && <li>排序 {strategy.ranking.field} / {strategy.ranking.direction} / Top {strategy.ranking.top_n}</li>}
              <li>最多 {strategy.portfolio.max_positions} 只 · 单票上限 {percent(strategy.portfolio.max_position_weight)} · 现金缓冲 {percent(strategy.portfolio.cash_buffer_weight)}</li>
            </ul>
          </div>

          <div className="rounded-lg bg-muted/40 p-3">
            <h4 className="font-medium">执行与成本</h4>
            <ul className="mt-1 space-y-1 text-[11px] text-muted-foreground">
              <li>{strategy.execution.rebalance} 调仓 · {strategy.execution.signal_price} 信号 · {strategy.execution.fill_price} 成交 · 延迟 {strategy.execution.signal_lag_bars} bar</li>
              <li>T+1 {strategy.execution.enforce_t_plus_one ? "开启" : "关闭"} · 整手 {strategy.execution.board_lot}</li>
              <li>佣金 {strategy.costs.commission_bps} bps（最低 {strategy.costs.minimum_commission}）· 卖出税 {strategy.costs.sell_tax_bps} bps</li>
              <li>过户费 {strategy.costs.transfer_fee_bps} bps · 滑点 {strategy.costs.slippage_bps} bps · {strategy.costs.rule_version}</li>
            </ul>
          </div>

          <div className="rounded-lg bg-muted/40 p-3">
            <h4 className="font-medium">风险与评估</h4>
            <ul className="mt-1 space-y-1 text-[11px] text-muted-foreground">
              <li>最大回撤停止 {percent(strategy.risk.max_drawdown_stop)} · 最大换手 {strategy.risk.max_turnover}</li>
              <li>训练至 {strategy.evaluation.train_end} · 验证至 {strategy.evaluation.validation_end} · 测试至 {strategy.evaluation.test_end}</li>
              <li>基准 {strategy.evaluation.benchmark} · walk-forward {strategy.evaluation.walk_forward ? "开启" : "关闭"}</li>
            </ul>
          </div>
        </div>
      )}

      {(data.version.defaults.length > 0 || data.version.diff.length > 0 || data.version.security_warnings.length > 0) && (
        <div className="grid gap-3 border-t border-border/60 px-4 py-3 text-[11px] md:grid-cols-2">
          {data.version.defaults.length > 0 && (
            <details>
              <summary className="cursor-pointer font-medium">可见默认值（{data.version.defaults.length}）</summary>
              <ul className="mt-2 space-y-1 text-muted-foreground">
                {data.version.defaults.map((item) => <li key={item.path}><span className="font-mono">{item.path}</span> = {item.value_json} · {item.reason}</li>)}
              </ul>
            </details>
          )}
          {data.version.diff.length > 0 && (
            <details open>
              <summary className="cursor-pointer font-medium">相对上一版本的变更（{data.version.diff.length}）</summary>
              <ul className="mt-2 space-y-1 text-muted-foreground">
                {data.version.diff.map((item) => <li key={item.path}><span className="font-mono">{item.path}</span> · {item.kind} · {item.before_json ?? "∅"} → {item.after_json ?? "∅"}</li>)}
              </ul>
            </details>
          )}
          {data.version.security_warnings.map((warning) => (
            <p key={`${warning.rule_id}:${warning.field}`} className="flex gap-1 text-amber-600">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{warning.message}
            </p>
          ))}
        </div>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border/60 px-4 py-3">
        <p className="text-[10px] text-muted-foreground">
          确认仅锁定此版本与数据口径，不会在 QE4 启动回测或交易。
        </p>
        {data.lifecycle_state === "confirmed" ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-600">
            <CheckCircle2 className="h-4 w-4" />已精确确认
          </span>
        ) : (
          <button
            type="button"
            disabled={disabled}
            onClick={() => void confirm()}
            aria-label="确认当前策略版本"
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {confirming ? "确认中…" : data.lifecycle_state === "awaiting_confirmation" ? "确认此版本" : stateLabel(data.lifecycle_state)}
          </button>
        )}
        {error && <p role="alert" className="w-full text-right text-[11px] text-destructive">{error}</p>}
      </footer>
    </section>
  );
}
