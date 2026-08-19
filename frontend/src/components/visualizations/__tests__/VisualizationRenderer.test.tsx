import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import { getChartTheme } from "@/lib/chart-theme";
import { VisualizationRenderer } from "../VisualizationRenderer";

vi.mock("@/components/charts/CandlestickChart", () => ({
  isIntradayTimeframe: (timeframe?: string) => ["1m", "5m", "15m", "30m", "1H"].includes(timeframe || ""),
  CandlestickChart: ({ data, height, timeframe, linkGroup, initialRange }: { data: unknown[]; height: number; timeframe?: string; linkGroup?: string | false; initialRange?: string }) => (
    <div data-testid="candlestick-chart" data-link-group={String(linkGroup)} data-initial-range={initialRange}>
      {data.length} bars at {height}px · {timeframe}
    </div>
  ),
}));

const spec = {
  schema_version: 1 as const,
  type: "candlestick_volume" as const,
  visualization_id: "kline_demo",
  data_ref: "kline_demo",
  title: "AAPL.US K-line",
  symbol: "AAPL.US",
  source: "yahoo",
  adjustment: "raw",
  timeframe: "1D",
  timezone: "UTC",
  actual_start: "2026-07-01",
  actual_end: "2026-07-21",
  bar_count: 1,
  dropped_bar_count: 3,
};

describe("VisualizationRenderer", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", class {
      private readonly callback: IntersectionObserverCallback;

      constructor(callback: IntersectionObserverCallback) {
        this.callback = callback;
      }

      observe(element: Element) {
        this.callback([{ isIntersecting: true, target: element } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
      }

      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
      readonly root = null;
      readonly rootMargin = "400px";
      readonly thresholds = [0];
    });
  });

  afterEach(() => {
    document.documentElement.lang = "en";
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("loads an inline chart and opens the expanded dialog", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_demo",
      type: "candlestick_volume",
      bars: [{ time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });
    render(<VisualizationRenderer runId="run-chart" visualizations={[spec]} />);

    expect(await screen.findByText("1 bars at 340px · 1D")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledWith("run-chart", "kline_demo", expect.any(AbortSignal));
    expect(screen.getByText("C 11.00")).toBeInTheDocument();
    expect(screen.getByText("dropped 3 invalid/duplicate bars")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /expand chart/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getAllByTestId("candlestick-chart")).toHaveLength(1);
    expect(screen.getByTestId("candlestick-chart")).toHaveAttribute("data-link-group", "false");
    expect(screen.getByTestId("candlestick-chart")).toHaveAttribute("data-initial-range", "1Y");
  });

  it("uses a five-day initial range for intraday chat charts", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_intraday",
      type: "candlestick_volume",
      timeframe: "5m",
      bars: [{ time: "2026-07-21T09:30:00", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });

    render(<VisualizationRenderer
      runId="run-intraday"
      visualizations={[{ ...spec, visualization_id: "kline_intraday", data_ref: "kline_intraday", timeframe: "5m" }]}
    />);

    expect(await screen.findByTestId("candlestick-chart")).toHaveAttribute("data-initial-range", "5D");
  });

  it("shows canonical provenance, watermark, units, fallback, and warnings", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_local",
      type: "candlestick_volume",
      bars: [{ time: "2026-08-14", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });
    render(
      <VisualizationRenderer
        runId="run-local"
        visualizations={[{
          ...spec,
          visualization_id: "kline_local",
          data_ref: "kline_local",
          source: "tencent",
          provider: "vendor_a",
          provider_version: "delivery-v1",
          canonical_version: "a".repeat(64),
          watermark: "2026-08-14",
          units: { vol: { value: "lot_100_shares" } },
          completeness: "complete",
          fallback: true,
          fallback_reason: "local_coverage_incomplete",
          warnings: ["identifier history is limited"],
        }]}
      />,
    );

    expect(await screen.findByText(/vendor_a@delivery-v1/)).toHaveTextContent(
      "canonical aaaaaaaaaaaa",
    );
    expect(screen.getByText(/vendor_a@delivery-v1/)).toHaveTextContent(
      "through 2026-08-14",
    );
    expect(screen.getByText(/vendor_a@delivery-v1/)).toHaveTextContent(
      "volume lot_100_shares",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Fallback source used: local coverage incomplete",
    );
    expect(screen.getByText(/Data warning:/)).toHaveTextContent(
      "identifier history is limited",
    );
  });

  it("uses the locale-aware chart theme for the header change color", async () => {
    document.documentElement.lang = "zh-CN";
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_colors",
      type: "candlestick_volume",
      bars: [
        { time: "2026-07-20", open: 9, high: 11, low: 8, close: 10, volume: 100 },
        { time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 200 },
      ],
    });

    render(<VisualizationRenderer
      runId="run-colors"
      visualizations={[{ ...spec, visualization_id: "kline_colors", data_ref: "kline_colors" }]}
    />);

    expect(await screen.findByText("+10.00%")).toHaveStyle({ color: getChartTheme().upColor });
  });
  it("isolates two simultaneous chat visualizations from global chart linking", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_demo",
      type: "candlestick_volume",
      bars: [{ time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });
    const secondSpec = {
      ...spec,
      visualization_id: "kline_second",
      data_ref: "kline_second",
      symbol: "MSFT.US",
    };

    render(
      <>
        <VisualizationRenderer runId="run-a" visualizations={[spec]} />
        <VisualizationRenderer runId="run-b" visualizations={[secondSpec]} />
      </>,
    );

    const charts = await screen.findAllByTestId("candlestick-chart");
    expect(charts).toHaveLength(2);
    for (const chart of charts) expect(chart).toHaveAttribute("data-link-group", "false");
  });

  it("defers loading until the chart approaches the viewport", async () => {
    let intersect: (() => void) | undefined;
    vi.stubGlobal("IntersectionObserver", class {
      private readonly callback: IntersectionObserverCallback;

      constructor(callback: IntersectionObserverCallback) {
        this.callback = callback;
        intersect = () => this.callback(
          [{ isIntersecting: true } as IntersectionObserverEntry],
          this as unknown as IntersectionObserver,
        );
      }

      observe() {}
      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
      readonly root = null;
      readonly rootMargin = "400px";
      readonly thresholds = [0];
    });
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_lazy",
      type: "candlestick_volume",
      bars: [{ time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });

    render(<VisualizationRenderer
      runId="run-lazy"
      visualizations={[{ ...spec, visualization_id: "kline_lazy", data_ref: "kline_lazy" }]}
    />);

    expect(api.getRunVisualization).not.toHaveBeenCalled();
    await act(async () => { intersect?.(); });
    expect(await screen.findByTestId("candlestick-chart")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledTimes(1);
  });

  it("reuses cached data when switching symbol tabs", async () => {
    vi.spyOn(api, "getRunVisualization").mockImplementation(async (_runId, visualizationId) => ({
      schema_version: 1,
      visualization_id: visualizationId,
      type: "candlestick_volume",
      bars: [{ time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    }));
    const first = { ...spec, visualization_id: "kline_cache_a", data_ref: "kline_cache_a" };
    const second = {
      ...spec,
      visualization_id: "kline_cache_b",
      data_ref: "kline_cache_b",
      symbol: "MSFT.US",
    };

    render(<VisualizationRenderer runId="run-cache" visualizations={[first, second]} />);
    await screen.findByTestId("candlestick-chart");
    await userEvent.click(screen.getByRole("button", { name: "MSFT.US" }));
    await waitFor(() => expect(api.getRunVisualization).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole("button", { name: "AAPL.US" }));
    await screen.findByTestId("candlestick-chart");

    expect(api.getRunVisualization).toHaveBeenCalledTimes(2);
    expect(api.getRunVisualization).toHaveBeenCalledWith("run-cache", "kline_cache_a", expect.any(AbortSignal));
    expect(api.getRunVisualization).toHaveBeenCalledWith("run-cache", "kline_cache_b", expect.any(AbortSignal));
  });

  it("aborts an in-flight chart request when the session changes", async () => {
    const signals = new Map<string, AbortSignal>();
    let resolveNew: ((value: Awaited<ReturnType<typeof api.getRunVisualization>>) => void) | undefined;
    vi.spyOn(api, "getRunVisualization").mockImplementation((runId, visualizationId, signal) => {
      if (signal) signals.set(runId, signal);
      return new Promise((resolve) => {
        if (runId === "run-new") resolveNew = resolve;
      });
    });
    const sessionSpec = { ...spec, visualization_id: "kline_session", data_ref: "kline_session" };
    const { rerender } = render(
      <VisualizationRenderer runId="run-old" visualizations={[sessionSpec]} />,
    );
    await waitFor(() => expect(api.getRunVisualization).toHaveBeenCalledTimes(1));

    rerender(<VisualizationRenderer runId="run-new" visualizations={[sessionSpec]} />);
    await waitFor(() => expect(api.getRunVisualization).toHaveBeenCalledTimes(2));
    expect(signals.get("run-old")?.aborted).toBe(true);

    await act(async () => {
      resolveNew?.({
        schema_version: 1,
        visualization_id: "kline_session",
        type: "candlestick_volume",
        bars: [{ time: "2026-07-22", open: 20, high: 22, low: 19, close: 21, volume: 200 }],
      });
    });
    expect(await screen.findByText("C 21.00")).toBeInTheDocument();
  });

  it("retries a failed visualization request with a fresh fetch", async () => {
    vi.spyOn(api, "getRunVisualization")
      .mockRejectedValueOnce(new Error("temporary chart failure"))
      .mockResolvedValueOnce({
        schema_version: 1,
        visualization_id: "kline_retry",
        type: "candlestick_volume",
        bars: [{ time: "2026-07-22", open: 20, high: 22, low: 19, close: 21, volume: 200 }],
      });

    render(<VisualizationRenderer
      runId="run-retry"
      visualizations={[{ ...spec, visualization_id: "kline_retry", data_ref: "kline_retry" }]}
    />);

    expect(await screen.findByText("temporary chart failure")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("C 21.00")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledTimes(2);
  });

  it("restores a historical chart from cache after remount", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_history",
      type: "candlestick_volume",
      bars: [{ time: "2026-07-22", open: 20, high: 22, low: 19, close: 21, volume: 200 }],
    });
    const historySpec = {
      ...spec,
      visualization_id: "kline_history",
      data_ref: "kline_history",
    };
    const first = render(
      <VisualizationRenderer runId="run-history" visualizations={[historySpec]} />,
    );
    await screen.findByText("C 21.00");
    first.unmount();

    render(<VisualizationRenderer runId="run-history" visualizations={[historySpec]} />);
    expect(await screen.findByText("C 21.00")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledTimes(1);
  });

  it("renders similarity scope, weights, scores, evidence, counterevidence, and degradation", async () => {
    const digest = "a".repeat(64);
    const rankingSpec = {
      schema_version: 1 as const,
      type: "similarity_ranking" as const,
      visualization_id: "similarity_ui",
      data_ref: "similarity_ui",
      title: "Two-stock similarity candidates",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH", "000858.SZ"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 1,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
      fallback_text: "Ranking unavailable",
    };
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "similarity_ui",
      type: "similarity_ranking",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      research_spec_id: `research_spec:${"b".repeat(64)}`,
      target_symbols: ["600519.SH", "000858.SZ"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
      candidates: [{
        rank: 1,
        symbol: "000568.SZ",
        combined_score: 0.82,
        coverage: 0.7,
        business_score: 0.91,
        factor_score: 0.76,
        price_volume_score: null,
        rank_stability: 0.875,
        evidence: ["same_industry:白酒"],
        counterevidence: ["price_volume_channel:unavailable"],
      }],
      excluded_symbol_count: 2,
    });

    render(<VisualizationRenderer runId="run-similarity" visualizations={[rankingSpec]} />);

    expect(await screen.findByText("000568.SZ")).toBeInTheDocument();
    expect(screen.getByText(/Targets 600519.SH, 000858.SZ/)).toBeInTheDocument();
    expect(screen.getByText("Business 30%")).toBeInTheDocument();
    expect(screen.getByText("Factor 40%")).toBeInTheDocument();
    expect(screen.getByText("Price/volume 30%")).toBeInTheDocument();
    expect(screen.getByText("+ same_industry:白酒")).toBeInTheDocument();
    expect(screen.getByText("− price_volume_channel:unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Degraded coverage · unavailable: price\/volume/)).toBeInTheDocument();
    expect(screen.getByText(/2 symbols excluded · result/)).toBeInTheDocument();
  });

  it("restores a historical similarity ranking from the shared visualization cache", async () => {
    const digest = "c".repeat(64);
    const rankingSpec = {
      schema_version: 1 as const,
      type: "similarity_ranking" as const,
      visualization_id: "similarity_history",
      data_ref: "similarity_history",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 1,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
    };
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1, visualization_id: "similarity_history", type: "similarity_ranking",
      similarity_run_id: `similarity_run:${digest}`, similarity_sha256: digest,
      research_spec_id: `research_spec:${"d".repeat(64)}`, target_symbols: ["600519.SH"],
      as_of: "2026-07-27", candidate_universe: "csi300@2026-07-27",
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
      candidates: [{ rank: 1, symbol: "000858.SZ", combined_score: 0.9, coverage: 1,
        business_score: 0.9, factor_score: 0.9, price_volume_score: 0.9,
        rank_stability: 1, evidence: ["support"], counterevidence: ["risk"] }],
      excluded_symbol_count: 1,
    });

    const first = render(<VisualizationRenderer runId="run-sim-history" visualizations={[rankingSpec]} />);
    await screen.findByText("000858.SZ");
    first.unmount();
    render(<VisualizationRenderer runId="run-sim-history" visualizations={[rankingSpec]} />);

    expect(await screen.findByText("000858.SZ")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledTimes(1);
  });

  it("fails closed when similarity payload weights differ from the persisted spec", async () => {
    const digest = "e".repeat(64);
    const rankingSpec = {
      schema_version: 1 as const,
      type: "similarity_ranking" as const,
      visualization_id: "similarity_weight_mismatch",
      data_ref: "similarity_weight_mismatch",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 1,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
    };
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "similarity_weight_mismatch",
      type: "similarity_ranking",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      research_spec_id: `research_spec:${"f".repeat(64)}`,
      target_symbols: ["600519.SH"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      weights: { business: 0.2, factor: 0.5, price_volume: 0.3 },
      candidates: [{ rank: 1, symbol: "000858.SZ", combined_score: 0.9, coverage: 1,
        business_score: 0.9, factor_score: 0.9, price_volume_score: 0.9,
        rank_stability: 1, evidence: ["support"], counterevidence: ["risk"] }],
      excluded_symbol_count: 1,
    });

    render(<VisualizationRenderer runId="run-sim-mismatch" visualizations={[rankingSpec]} />);

    expect(await screen.findByText(
      "Similarity payload did not match its content-bound manifest.",
    )).toBeInTheDocument();
    expect(screen.queryByText("000858.SZ")).not.toBeInTheDocument();
  });

});
