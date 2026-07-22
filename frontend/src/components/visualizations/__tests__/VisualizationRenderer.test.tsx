import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import { VisualizationRenderer } from "../VisualizationRenderer";

vi.mock("@/components/charts/CandlestickChart", () => ({
  CandlestickChart: ({ data, height, timeframe, linkGroup }: { data: unknown[]; height: number; timeframe?: string; linkGroup?: string | false }) => (
    <div data-testid="candlestick-chart" data-link-group={String(linkGroup)}>
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
  afterEach(() => vi.restoreAllMocks());

  it("loads an inline chart and opens the expanded dialog", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue({
      schema_version: 1,
      visualization_id: "kline_demo",
      type: "candlestick_volume",
      bars: [{ time: "2026-07-21", open: 10, high: 12, low: 9, close: 11, volume: 100 }],
    });
    render(<VisualizationRenderer runId="run-chart" visualizations={[spec]} />);

    expect(await screen.findByText("1 bars at 340px · 1D")).toBeInTheDocument();
    expect(api.getRunVisualization).toHaveBeenCalledWith("run-chart", "kline_demo");
    expect(screen.getByText("C 11.00")).toBeInTheDocument();
    expect(screen.getByText("dropped 3 invalid/duplicate bars")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /expand chart/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getAllByTestId("candlestick-chart")).toHaveLength(1);
    expect(screen.getByTestId("candlestick-chart")).toHaveAttribute("data-link-group", "false");
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

});
