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

  it("keeps a content-bound similarity ranking spec", () => {
    const digest = "a".repeat(64);
    expect(parseVisualizationSpecs([{
      schema_version: 1,
      type: "similarity_ranking",
      visualization_id: "similarity_demo",
      data_ref: "similarity_demo",
      title: "Two-stock similarity candidates",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH", "000858.SZ"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 10,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
      fallback_text: "Ranking unavailable",
      ignored: "value",
    }])).toEqual([{
      schema_version: 1,
      type: "similarity_ranking",
      visualization_id: "similarity_demo",
      data_ref: "similarity_demo",
      title: "Two-stock similarity candidates",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH", "000858.SZ"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 10,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
      fallback_text: "Ranking unavailable",
    }]);
  });

  it("rejects mismatched similarity identities, invalid weights, and duplicate targets", () => {
    const digest = "a".repeat(64);
    const valid = {
      schema_version: 1,
      type: "similarity_ranking",
      visualization_id: "similarity_demo",
      data_ref: "similarity_demo",
      similarity_run_id: `similarity_run:${digest}`,
      similarity_sha256: digest,
      target_symbols: ["600519.SH", "000858.SZ"],
      as_of: "2026-07-27",
      candidate_universe: "csi300@2026-07-27",
      candidate_count: 10,
      weights: { business: 0.3, factor: 0.4, price_volume: 0.3 },
    };
    expect(parseVisualizationSpecs([
      { ...valid, similarity_sha256: "b".repeat(64) },
      { ...valid, weights: { business: 0.3, factor: 0.4, price_volume: 0.4 } },
      { ...valid, target_symbols: ["600519.SH", "600519.SH"] },
      { ...valid, candidate_count: 0 },
      { ...valid, as_of: "2026-02-31" },
    ])).toEqual([]);
  });

  it("keeps only a complete, session-bound strategy confirmation spec", () => {
    const version = `strategy-version:${"a".repeat(64)}`;
    const event = `strategy-state:${"b".repeat(64)}`;
    const hash = "c".repeat(64);
    const valid = {
      schema_version: 1,
      type: "strategy_confirmation",
      visualization_id: "strategy_demo",
      data_ref: "strategy_demo",
      title: "低波动月度策略",
      stream_id: "session-demo",
      version_id: version,
      version_number: 1,
      parent_version_id: null,
      head_event_id: event,
      head_revision: 2,
      confirmation_hash: hash,
      fallback_text: "策略确认卡不可用",
      ignored: "must-not-survive",
    };
    expect(parseVisualizationSpecs([valid])).toEqual([{
      schema_version: 1,
      type: "strategy_confirmation",
      visualization_id: "strategy_demo",
      data_ref: "strategy_demo",
      title: "低波动月度策略",
      stream_id: "session-demo",
      version_id: version,
      version_number: 1,
      parent_version_id: null,
      head_event_id: event,
      head_revision: 2,
      confirmation_hash: hash,
      fallback_text: "策略确认卡不可用",
    }]);
    expect(parseVisualizationSpecs([
      { ...valid, stream_id: "../other" },
      { ...valid, version_id: `strategy-version:${"d".repeat(63)}` },
      { ...valid, parent_version_id: `strategy-version:${"e".repeat(64)}` },
      { ...valid, confirmation_hash: "short" },
      { ...valid, data_ref: "other" },
    ])).toEqual([]);
  });
});
