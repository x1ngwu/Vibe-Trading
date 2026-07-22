import { parseVisualizationSpecs } from "../visualizations";

describe("parseVisualizationSpecs", () => {
  it("keeps supported, path-safe chart specs", () => {
    expect(parseVisualizationSpecs([{
      schema_version: 1,
      type: "candlestick_volume",
      visualization_id: "kline_abc",
      data_ref: "kline_abc",
      symbol: "600519.SH",
      timezone: "Asia/Shanghai",
      effective_fetch_start: "2026-07-15",
      effective_fetch_end: "2026-07-21",
      retention_policy: "latest_contiguous_up_to_5000_bars",
      bar_count: 252,
      dropped_bar_count: 3,
      truncated: true,
      ignored: "value",
    }])).toEqual([{
      schema_version: 1,
      type: "candlestick_volume",
      visualization_id: "kline_abc",
      data_ref: "kline_abc",
      symbol: "600519.SH",
      timezone: "Asia/Shanghai",
      effective_fetch_start: "2026-07-15",
      effective_fetch_end: "2026-07-21",
      retention_policy: "latest_contiguous_up_to_5000_bars",
      bar_count: 252,
      dropped_bar_count: 3,
      truncated: true,
    }]);
  });

  it("rejects unknown schemas and unsafe references", () => {
    expect(parseVisualizationSpecs([
      { schema_version: 2, type: "candlestick_volume", visualization_id: "x", data_ref: "x" },
      { schema_version: 1, type: "candlestick_volume", visualization_id: "../x", data_ref: "../x" },
      { schema_version: 1, type: "html", visualization_id: "x", data_ref: "x" },
    ])).toEqual([]);
  });
});
