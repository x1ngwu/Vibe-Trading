import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PriceBar } from "@/lib/api";
import { getChartTheme } from "@/lib/chart-theme";
import { CandlestickChart } from "../CandlestickChart";

const chartHarness = vi.hoisted(() => {
  const handlers = new Map<string, () => void>();
  const state: {
    zoom: Array<{ start?: number; end?: number }>;
  } = { zoom: [] };
  const chart = {
    group: "",
    setOption: vi.fn(),
    getOption: vi.fn(),
    on: vi.fn((event: string, handler: () => void) => handlers.set(event, handler)),
    off: vi.fn((event: string) => handlers.delete(event)),
    resize: vi.fn(),
    dispose: vi.fn(),
  };
  return { chart, handlers, state };
});

vi.mock("@/lib/echarts", () => ({
  CHART_GROUP: "quant-charts",
  connectCharts: vi.fn(),
  echarts: { init: vi.fn(() => chartHarness.chart) },
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false, toggle: vi.fn() }),
}));

function bar(time: string, close: number): PriceBar {
  return { time, open: close - 1, high: close + 1, low: close - 2, close, volume: 100 };
}

describe("CandlestickChart interactions", () => {
  beforeEach(() => {
    document.documentElement.lang = "en";
    chartHarness.handlers.clear();
    chartHarness.state.zoom = [];
    vi.clearAllMocks();
    chartHarness.chart.getOption.mockImplementation(() => ({ dataZoom: chartHarness.state.zoom }));
    chartHarness.chart.setOption.mockImplementation((option: { dataZoom?: Array<{ start?: number; end?: number }> }) => {
      chartHarness.state.zoom = option.dataZoom || [];
    });
  });

  afterEach(() => {
    document.documentElement.lang = "en";
  });

  it("keeps a user-selected zoom when an indicator is changed", async () => {
    const data = [bar("2025-01-01", 10), bar("2026-07-22", 11)];
    render(<CandlestickChart data={data} />);
    await waitFor(() => expect(chartHarness.chart.setOption).toHaveBeenCalled());

    chartHarness.state.zoom = [{ start: 41, end: 76 }];
    act(() => chartHarness.handlers.get("dataZoom")?.());
    const callsBeforeToggle = chartHarness.chart.setOption.mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: /indicators/i }));
    await userEvent.click(screen.getByLabelText("MA10"));
    await waitFor(() => expect(chartHarness.chart.setOption.mock.calls.length).toBeGreaterThan(callsBeforeToggle));

    const lastOption = chartHarness.chart.setOption.mock.calls.at(-1)?.[0] as {
      dataZoom: Array<{ start?: number; end?: number }>;
    };
    expect(lastOption.dataZoom[0]).toMatchObject({ start: 41, end: 76 });
  });

  it("uses the locale theme consistently for candles and volume", async () => {
    document.documentElement.lang = "zh-CN";
    const data = [
      bar("2026-07-21", 11),
      { time: "2026-07-22", open: 12, high: 13, low: 10, close: 11, volume: 200 },
    ];
    render(<CandlestickChart data={data} />);
    await waitFor(() => expect(chartHarness.chart.setOption).toHaveBeenCalled());

    const lastOption = chartHarness.chart.setOption.mock.calls.at(-1)?.[0] as {
      series: Array<{
        name: string;
        itemStyle?: { color: string; color0: string };
        data?: Array<{ itemStyle?: { color: string } }>;
      }>;
    };
    const theme = getChartTheme();
    const candle = lastOption.series.find((series) => series.name === "K");
    const volume = lastOption.series.find((series) => series.name === "Vol");

    expect(candle?.itemStyle).toMatchObject({ color: theme.upColor, color0: theme.downColor });
    expect(volume?.data?.[0].itemStyle?.color).toBe(theme.volumeUp);
    expect(volume?.data?.[1].itemStyle?.color).toBe(theme.volumeDown);
  });
});
