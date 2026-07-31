import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, type BacktestResultPayload } from "@/lib/api";
import { BacktestResultCard } from "../BacktestResultCard";

vi.mock("@/components/charts/EquityChart", () => ({
  EquityChart: ({ data, trades }: { data: unknown[]; trades: unknown[] }) => (
    <div data-testid="equity-chart">{data.length} equity · {trades.length} trades</div>
  ),
}));

const spec = {
  schema_version: 1 as const,
  type: "backtest_result" as const,
  visualization_id: "backtest_aaaaaaaaaaaaaaaaaaaaaaaa",
  data_ref: "backtest_aaaaaaaaaaaaaaaaaaaaaaaa",
  title: "月度 Top 5 · 回测结果",
  stream_id: "session-backtest",
  strategy_version_id: `strategy-version:${"1".repeat(64)}`,
  strategy_version_number: 2,
  job_id: `backtest-job:${"2".repeat(64)}`,
  fallback_text: "回测状态暂时不可用。",
};

const completed: BacktestResultPayload = {
  schema_version: "vibe.backtest-product.v1",
  visualization_id: spec.visualization_id,
  type: "backtest_result",
  stream_id: spec.stream_id,
  strategy_version_id: spec.strategy_version_id,
  strategy_version_number: 2,
  job_id: spec.job_id,
  status: "completed",
  submitted_at: "2026-07-31T06:00:00Z",
  started_at: "2026-07-31T06:00:01Z",
  finished_at: "2026-07-31T06:00:02Z",
  run_id: `backtest-record:${"3".repeat(64)}`,
  metrics: {
    total_return: 0.1234,
    annualized_return: null,
    max_drawdown: -0.0567,
    turnover: 1.25,
    trade_count: 2,
  },
  equity: [
    { time: "2026-01-01", equity: 100_000_000, drawdown: 0 },
    { time: "2026-01-02", equity: 112_340_000, drawdown: 0 },
  ],
  trades: [
    { time: "2026-01-01", symbol: "600001.SH", side: "BUY", price: 10, qty: 100 },
    { time: "2026-01-02", symbol: "600001.SH", side: "SELL", price: 11, qty: 100 },
  ],
  diagnostics: [],
  snapshot_sha256: "4".repeat(64),
  ledger_sha256: "5".repeat(64),
  engine_commit: "6".repeat(40),
  truncated_equity: false,
  truncated_trades: false,
  truncated_diagnostics: false,
};

describe("BacktestResultCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("restores completed metrics, equity/trade markers, and compares an older version", async () => {
    vi.spyOn(api, "getBacktest").mockResolvedValue(completed);
    vi.spyOn(api, "listBacktests").mockResolvedValue([{
      job_id: `backtest-job:${"7".repeat(64)}`,
      owner_scope: "household:v1",
      idempotency_key: "older",
      request_sha256: "8".repeat(64),
      strategy_stream_id: spec.stream_id,
      strategy_version_id: `strategy-version:${"9".repeat(64)}`,
      status: "completed",
      submitted_at: "2026-07-30T06:00:00Z",
      started_at: "2026-07-30T06:00:01Z",
      finished_at: "2026-07-30T06:00:02Z",
      run_id: `backtest-record:${"a".repeat(64)}`,
      diagnostic: null,
      cancel_requested: false,
      attempts: 1,
    }]);
    vi.spyOn(api, "compareBacktests").mockResolvedValue({
      schema_version: "vibe.backtest-comparison.v1",
      stream_id: spec.stream_id,
      left_job_id: `backtest-job:${"7".repeat(64)}`,
      right_job_id: spec.job_id,
      left_version_id: `strategy-version:${"9".repeat(64)}`,
      right_version_id: spec.strategy_version_id,
      deltas: [{
        metric: "total_return",
        left: 0.1,
        right: 0.1234,
        delta: 0.0234,
      }],
    });

    render(<BacktestResultCard spec={spec} />);

    expect(await screen.findByText("12.34%")).toBeInTheDocument();
    expect(screen.getByTestId("equity-chart")).toHaveTextContent("2 equity · 2 trades");
    expect(screen.getByText(/三角\/图钉标记买卖成交/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "与旧版本比较" }));
    expect(await screen.findByText("total_return")).toBeInTheDocument();
    expect(screen.getByText("2.34%")).toBeInTheDocument();
  });

  it("offers keyboard-accessible cancellation for a running job", async () => {
    const running: BacktestResultPayload = {
      ...completed,
      status: "running",
      finished_at: null,
      run_id: null,
      metrics: null,
      equity: [],
      trades: [],
      snapshot_sha256: null,
      ledger_sha256: null,
      engine_commit: null,
    };
    const cancelled: BacktestResultPayload = {
      ...running,
      status: "cancelled",
      finished_at: "2026-07-31T06:00:03Z",
      diagnostics: [{
        code: "WORKER_CANCELLED",
        message: "worker was cancelled",
        trade_date: null,
        kind: null,
      }],
    };
    vi.spyOn(api, "getBacktest")
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(cancelled);
    vi.spyOn(api, "cancelBacktest").mockResolvedValue({
      job_id: spec.job_id,
      owner_scope: "household:v1",
      idempotency_key: "cancel",
      request_sha256: "8".repeat(64),
      strategy_stream_id: spec.stream_id,
      strategy_version_id: spec.strategy_version_id,
      status: "cancelled",
      submitted_at: running.submitted_at,
      started_at: running.started_at,
      finished_at: cancelled.finished_at,
      run_id: null,
      diagnostic: { code: "WORKER_CANCELLED", message: "worker was cancelled", stderr: "" },
      cancel_requested: true,
      attempts: 1,
    });
    vi.spyOn(api, "backtestSseUrl").mockResolvedValue("/events");
    vi.stubGlobal("EventSource", class {
      onerror: ((event: Event) => void) | null = null;
      addEventListener() {}
      close() {}
    });

    render(<BacktestResultCard spec={spec} />);
    const cancel = await screen.findByRole("button", { name: "Cancel backtest" });
    cancel.focus();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(api.cancelBacktest).toHaveBeenCalledWith(spec.stream_id, spec.job_id));
    expect(await screen.findByText("回测已取消")).toBeInTheDocument();
    expect(screen.getByText("WORKER_CANCELLED")).toBeInTheDocument();
  });

  it("applies a terminal failure delivered by the authenticated SSE stream", async () => {
    const running: BacktestResultPayload = {
      ...completed,
      status: "running",
      finished_at: null,
      run_id: null,
      metrics: null,
      equity: [],
      trades: [],
      snapshot_sha256: null,
      ledger_sha256: null,
      engine_commit: null,
    };
    const failed: BacktestResultPayload = {
      ...running,
      status: "failed",
      finished_at: "2026-07-31T06:00:03Z",
      diagnostics: [{
        code: "WORKER_TIMEOUT",
        message: "worker exceeded the hard timeout",
        trade_date: null,
        kind: null,
      }],
    };
    vi.spyOn(api, "getBacktest").mockResolvedValue(running);
    vi.spyOn(api, "backtestSseUrl").mockResolvedValue("/events?ticket=one-shot");
    let instance: {
      listeners: Map<string, (event: MessageEvent) => void>;
      close: ReturnType<typeof vi.fn>;
    } | undefined;
    vi.stubGlobal("EventSource", class {
      listeners = new Map<string, (event: MessageEvent) => void>();
      close = vi.fn();
      onerror: ((event: Event) => void) | null = null;

      constructor() {
        instance = this;
      }

      addEventListener(name: string, listener: EventListener) {
        this.listeners.set(name, listener as (event: MessageEvent) => void);
      }
    });

    render(<BacktestResultCard spec={spec} />);
    await waitFor(() => expect(instance).toBeDefined());
    await act(async () => {
      instance?.listeners.get("backtest")?.(
        new MessageEvent("backtest", { data: JSON.stringify(failed) }),
      );
    });

    expect(await screen.findByText("回测失败")).toBeInTheDocument();
    expect(screen.getByText("WORKER_TIMEOUT")).toBeInTheDocument();
    expect(screen.getByText("worker exceeded the hard timeout")).toBeInTheDocument();
  });
});
