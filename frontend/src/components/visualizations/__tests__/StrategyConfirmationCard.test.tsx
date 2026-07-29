import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, type StrategyConfirmationRunVisualization } from "@/lib/api";
import type { StrategyConfirmationVisualizationSpec } from "@/types/agent";
import { StrategyConfirmationCard } from "../StrategyConfirmationCard";

const versionDigest = "a".repeat(64);
const eventDigest = "b".repeat(64);
const confirmationHash = "c".repeat(64);
const snapshotDigest = "d".repeat(64);
const strategyDigest = "e".repeat(64);
const researchDigest = "f".repeat(64);

const spec: StrategyConfirmationVisualizationSpec = {
  schema_version: 1,
  type: "strategy_confirmation",
  visualization_id: "strategy_demo",
  data_ref: "strategy_demo",
  title: "低波动月度策略",
  stream_id: "session-card",
  version_id: `strategy-version:${versionDigest}`,
  version_number: 2,
  parent_version_id: `strategy-version:${"9".repeat(64)}`,
  head_event_id: `strategy-state:${eventDigest}`,
  head_revision: 4,
  confirmation_hash: confirmationHash,
  fallback_text: "策略确认卡不可用",
};

const objectRef = (objectType: string, digest: string) => ({
  schema_version: "1.0" as const,
  object_type: objectType,
  object_id: `${objectType}:${digest}`,
  content_sha256: digest,
});

const strategy = {
  object_type: "strategy_spec" as const,
  research_spec_ref: objectRef("research_spec", researchDigest),
  similarity_run_ref: null,
  data_snapshot_ref: objectRef("data_snapshot_ref", snapshotDigest),
  title: "低波动月度策略",
  universe_symbols: ["600519.SH", "000858.SZ", "000001.SZ"],
  signals: [{
    field: "close",
    operator: "gt",
    value: 0,
    lookback_days: 1,
    consecutive_days: 1,
  }],
  ranking: { field: "volatility_20d", direction: "ascending" as const, top_n: 2 },
  portfolio: {
    weighting: "equal" as const,
    max_positions: 2,
    max_position_weight: 0.45,
    cash_buffer_weight: 0.1,
  },
  execution: {
    signal_price: "close" as const,
    fill_price: "next_open" as const,
    signal_lag_bars: 1,
    rebalance: "monthly" as const,
    enforce_t_plus_one: true,
    board_lot: 100,
  },
  costs: {
    commission_bps: 3,
    minimum_commission: 5,
    sell_tax_bps: 5,
    transfer_fee_bps: 0.1,
    slippage_bps: 5,
    rule_version: "cn-equity-2025-01-01",
  },
  risk: { max_drawdown_stop: 0.1, max_turnover: 6 },
  evaluation: {
    train_end: "2024-01-01",
    validation_end: "2025-01-01",
    test_end: "2025-06-30",
    benchmark: "000300.SH",
    walk_forward: true,
  },
};

function payload(
  state: StrategyConfirmationRunVisualization["lifecycle_state"] = "awaiting_confirmation",
): StrategyConfirmationRunVisualization {
  return {
    schema_version: 1,
    type: "strategy_confirmation",
    visualization_id: "strategy_demo",
    stream_id: "session-card",
    lifecycle_state: state,
    version: {
      version_id: spec.version_id,
      content_sha256: versionDigest,
      stream_id: "session-card",
      owner_scope: "household:v1",
      version_number: 2,
      parent_version_id: spec.parent_version_id,
      draft_status: "ready",
      proposal: {},
      strategy_spec_ref: objectRef("strategy_spec", strategyDigest),
      strategy,
      defaults: [{
        path: "costs",
        value_json: "{\"commission_bps\":3}",
        reason: "首版固定使用版本化 A 股费用。",
      }],
      clarifications: [],
      security_warnings: [],
      diff: [{
        path: "$.strategy.ranking.top_n",
        kind: "replace",
        before_json: "5",
        after_json: "2",
      }],
      created_at: "2026-07-29T12:00:00Z",
    },
    head: {
      stream_id: "session-card",
      version_id: spec.version_id,
      event_id: state === "confirmed" ? `strategy-state:${"8".repeat(64)}` : spec.head_event_id,
      revision: state === "confirmed" ? 5 : 4,
      state: state === "confirmed" ? "confirmed" : "awaiting_confirmation",
    },
    data_basis: {
      snapshot_ref: objectRef("data_snapshot_ref", snapshotDigest),
      as_of: "2025-06-30",
      start_date: "2023-01-03",
      end_date: "2025-06-30",
      frequency: "1d",
      adjustment: "qfq",
      requested_sources: ["fixture"],
      actual_sources: {
        "600519.SH": "fixture",
        "000858.SZ": "fixture",
        "000001.SZ": "fixture",
      },
      anomalies: [],
    },
    card: {
      card_id: `strategy-confirmation:${confirmationHash}`,
      confirmation_hash: confirmationHash,
      stream_id: "session-card",
      version_id: spec.version_id,
      version_number: 2,
      parent_version_id: spec.parent_version_id,
      strategy_spec_ref: objectRef("strategy_spec", strategyDigest),
      strategy,
      defaults: [],
      security_warnings: [],
      diff: [],
      issued_at: "2026-07-29T12:00:00Z",
      expires_at: "2026-07-29T12:15:00Z",
    },
    receipt: state === "confirmed" ? {
      receipt_id: `strategy-receipt:${"7".repeat(64)}`,
      stream_id: "session-card",
      version_id: spec.version_id,
      confirmation_hash: confirmationHash,
      idempotency_key: "strategy-confirm:test",
      actor_id: "household-user",
      confirmed_at: "2026-07-29T12:01:00Z",
    } : null,
  };
}

describe("StrategyConfirmationCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the complete basis/default/diff and requires an explicit click", async () => {
    vi.spyOn(api, "getRunVisualization").mockResolvedValue(payload());
    vi.spyOn(api, "confirmStrategy").mockResolvedValue(payload("confirmed"));
    render(<StrategyConfirmationCard runId="run-card" spec={spec} />);

    expect(await screen.findByText("等待确认")).toBeInTheDocument();
    expect(screen.getByText(/600519.SH, 000858.SZ, 000001.SZ/)).toBeInTheDocument();
    expect(screen.getByText("1d / qfq")).toBeInTheDocument();
    expect(screen.getByText(/Top 2/)).toBeInTheDocument();
    expect(screen.getByText(/T\+1 开启/)).toBeInTheDocument();
    expect(screen.getByText(/最大回撤停止 10.0%/)).toBeInTheDocument();
    expect(screen.getByText("可见默认值（1）")).toBeInTheDocument();
    expect(screen.getByText("相对上一版本的变更（1）")).toBeInTheDocument();

    await userEvent.keyboard("{Enter}");
    expect(api.confirmStrategy).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "确认当前策略版本" }));
    expect(api.confirmStrategy).toHaveBeenCalledWith(
      "session-card",
      "run-card",
      "strategy_demo",
      expect.objectContaining({
        expected_head: payload().head,
        confirmation_hash: confirmationHash,
        idempotency_key: expect.stringMatching(/^strategy-confirm:strategy_demo:/),
      }),
    );
    expect(await screen.findByText("已精确确认")).toBeInTheDocument();
  });

  it("disables an expired card and rejects a mismatched payload", async () => {
    vi.spyOn(api, "getRunVisualization")
      .mockResolvedValueOnce(payload("expired"))
      .mockResolvedValueOnce({ ...payload(), stream_id: "other-session" });
    const first = render(<StrategyConfirmationCard runId="run-expired" spec={spec} />);
    expect(await screen.findByRole("button", { name: "确认当前策略版本" })).toBeDisabled();
    expect(screen.getAllByText("已过期")).toHaveLength(2);
    first.unmount();

    render(<StrategyConfirmationCard runId="run-mismatch" spec={spec} />);
    expect(await screen.findByText("策略确认卡与消息中的版本标识不一致。")).toBeInTheDocument();
  });
});
