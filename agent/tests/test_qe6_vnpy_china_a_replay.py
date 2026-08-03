"""QE6-3 independent China-A rule reconciliation."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.quant_engine import (
    CnEquityAccount,
    CnEquityFeeSchedule,
    CnEquityOrder,
    EngineIdentity,
    QUANTAXIS_ENGINE_VERSION,
    QUANTAXIS_SOURCE_SHA256,
    QuantaxisAdapter,
    QuantaxisBacktestEvent,
    QuantaxisBacktestResult,
    QuantaxisBacktestWorkerResult,
    QuantaxisPositionSnapshot,
    QuantaxisRiskAudit,
    QuantaxisRiskPolicy,
    VNPY_ENGINE_COMMIT,
    VnpyOperationError,
    VnpyOracleAdapter,
    WorkerConfig,
    WorkerRunner,
)
from tests.test_qe5_quantaxis_backtest import _WorkerRunner, _confirmed_chain
from tests.test_qe6_vnpy_event_path import COMMON_DIR, WORKER_DIR, _compilation
from tests.test_qe6_vnpy_ordinary_replay import _boundary

for path in (str(COMMON_DIR), str(WORKER_DIR)):
    if path not in os.sys.path:
        os.sys.path.insert(0, path)

from china_a_replay import build_china_a_replay_handler  # noqa: E402
from worker_runtime import WorkerError  # noqa: E402


class _FakeRunner:
    def __init__(self, mutate_payload: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config = SimpleNamespace(engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT))
        self.mutate_payload = mutate_payload

    def run(self, **kwargs: Any) -> Any:
        payload = deepcopy(kwargs["payload"])
        if self.mutate_payload is not None:
            self.mutate_payload(payload)
        try:
            result = build_china_a_replay_handler(_boundary)(
                payload,
                {"path": kwargs["snapshot_path"], "sha256": kwargs["snapshot_sha256"]},
            )
            response = {"status": "ok", "error": None, "result": result}
        except WorkerError as exc:
            response = {"status": "error", "error": {"code": exc.code, "message": str(exc)}, "result": None}
        return SimpleNamespace(response=response)


def _inputs(tmp_path: Path):
    snapshot_path, snapshot, compilation, version, head, card, receipt = _confirmed_chain(tmp_path)
    qe5 = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )
    return snapshot_path, snapshot, compilation, qe5


def _special_inputs(tmp_path: Path):
    snapshot_path, snapshot, compilation = _compilation(tmp_path)
    request = compilation.engine_request.payload
    rules = CnEquityFeeSchedule(
        commission_tenths_bps=30,
        minimum_commission_fen=500,
        sell_tax_tenths_bps=50,
        transfer_fee_tenths_bps=1,
        rule_version="cn-equity-2025-01-01",
    )
    day1 = snapshot.payload.start_date
    day2 = date(2025, 1, 3)
    day3 = date(2025, 1, 6)
    symbol = "600001.SH"
    account = CnEquityAccount(
        ledger_id=f"quantaxis:{request.request_id}",
        data_snapshot_sha256=snapshot.payload.snapshot_sha256,
        opening_date=day1,
        initial_cash_fen=10_000_000,
        rules=rules,
    )
    orders = (
        CnEquityOrder(order_id="buy", trade_date=day1, symbol=symbol, side="buy", requested_shares=100, price_fen=1_000),
        CnEquityOrder(order_id="t1", trade_date=day1, symbol=symbol, side="sell", requested_shares=100, price_fen=1_000),
        CnEquityOrder(order_id="suspended", trade_date=day1, symbol="000002.SZ", side="buy", requested_shares=100, price_fen=500, market_state="suspended"),
        CnEquityOrder(order_id="limit-up", trade_date=day1, symbol="000002.SZ", side="buy", requested_shares=100, price_fen=500, market_state="locked_limit_up"),
        CnEquityOrder(order_id="limit-down", trade_date=day2, symbol=symbol, side="sell", requested_shares=100, price_fen=1_000, market_state="locked_limit_down"),
    )
    events: list[QuantaxisBacktestEvent] = []
    for order in orders:
        account.submit_order(order)
        events.append(QuantaxisBacktestEvent(event="order", trade_date=order.trade_date, order=order, market_rule_id="qe6-special-v1"))
    account.apply_share_split(trade_date=day2, symbol=symbol, multiplier_numerator=2, multiplier_denominator=1, mark_prices_fen={symbol: 500})
    events.append(QuantaxisBacktestEvent(event="share_split", trade_date=day2, symbol=symbol, multiplier_numerator=2, multiplier_denominator=1, mark_prices_fen={symbol: 500}))
    account.accrue_dividend(trade_date=day2, symbol=symbol, entitled_shares=200, cash_per_share_fen=5, mark_prices_fen={symbol: 500})
    events.append(QuantaxisBacktestEvent(event="dividend_ex", trade_date=day2, symbol=symbol, entitled_shares=200, cash_per_share_fen=5, mark_prices_fen={symbol: 500}))
    account.pay_dividend(trade_date=day3, symbol=symbol, mark_prices_fen={symbol: 510})
    events.append(QuantaxisBacktestEvent(event="dividend_pay", trade_date=day3, symbol=symbol, mark_prices_fen={symbol: 510}))
    mark = account.mark(trade_date=day3, mark_prices_fen={symbol: 510})
    events.append(QuantaxisBacktestEvent(event="mark", trade_date=day3, mark_prices_fen={symbol: 510}))
    worker = QuantaxisBacktestWorkerResult(
        snapshot_sha256=snapshot.payload.snapshot_sha256,
        engine_request_id=request.request_id,
        execution_plan_sha256=compilation.plan.content_sha256,
        backtest_input_sha256="c" * 64,
        rule_table_version="qe6-special-v1",
        fee_schedule=rules,
        slippage_tenths_bps=50,
        risk_policy=QuantaxisRiskPolicy(),
        opening_date=day1,
        initial_cash_fen=10_000_000,
        board_lot=100,
        events=tuple(events),
        engine_position_snapshots=(QuantaxisPositionSnapshot(trade_date=day3, positions=mark.positions, cash_fen=mark.cash_fen, dividend_receivable_fen=0, equity_fen=mark.equity_fen),),
        risk_audit=(QuantaxisRiskAudit(trade_date=day3, peak_equity_fen=mark.equity_fen, drawdown_ppm=0, cumulative_purchase_notional_fen=100_000, purchase_turnover_ppm=10_000, drawdown_halted=False),),
        engine_version=QUANTAXIS_ENGINE_VERSION,
        source_sha256=QUANTAXIS_SOURCE_SHA256,
    )
    return snapshot_path, snapshot, compilation, QuantaxisBacktestResult(worker=worker, ledger=account.ledger())


def test_qe6_3_reconciles_t1_fees_and_corporate_actions(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, qe5 = _inputs(tmp_path)
    replay = VnpyOracleAdapter(_FakeRunner()).reconcile_china_a(  # type: ignore[arg-type]
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
    )
    events = [item.event for item in qe5.ledger.entries]
    assert "dividend_ex" in events and "dividend_pay" in events
    assert replay.worker.reconciled_entries == len(qe5.ledger.entries) - 1


def test_qe6_3_fails_at_changed_rule_input(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, qe5 = _inputs(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        order = next(item for item in payload["events"] if item["event"] == "order")
        order["order"]["price_fen"] += 1

    with pytest.raises(VnpyOperationError) as error:
        VnpyOracleAdapter(_FakeRunner(mutate)).reconcile_china_a(  # type: ignore[arg-type]
            compilation=compilation,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            qe5=qe5,
        )
    assert error.value.code == "CHINA_A_REPLAY_DIVERGENCE"


def test_qe6_3_reconciles_t1_market_states_split_and_dividend(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, qe5 = _special_inputs(tmp_path)
    replay = VnpyOracleAdapter(_FakeRunner()).reconcile_china_a(  # type: ignore[arg-type]
        compilation=compilation, snapshot=snapshot, snapshot_path=snapshot_path, qe5=qe5
    )
    reasons = {entry.reason for entry in qe5.ledger.entries}
    assert {"T1_LOCKED", "SUSPENDED", "LIMIT_UP_LOCKED", "LIMIT_DOWN_LOCKED"} <= reasons
    buy = next(entry for entry in qe5.ledger.entries if entry.event == "buy")
    split = next(entry for entry in qe5.ledger.entries if entry.event == "share_split")
    dividend = next(entry for entry in qe5.ledger.entries if entry.event == "dividend_ex")
    assert buy.fees.total_fen == 501
    assert split.position_delta == {"600001.SH": 100}
    assert dividend.dividend_receivable_delta_fen == 1_000
    assert replay.worker.reconciled_entries == len(qe5.ledger.entries) - 1


@pytest.mark.integration
def test_qe6_3_real_pinned_vnpy_china_a_replay(tmp_path: Path) -> None:
    python_text = os.environ.get("VIBE_QE0_VNPY_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_VNPY_PYTHON to run pinned vn.py QE6-3")
    snapshot_path, snapshot, compilation, qe5 = _inputs(tmp_path)
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(WORKER_DIR / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_DIR.resolve(),
    )
    replay = VnpyOracleAdapter(runner).reconcile_china_a(
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
    )
    assert replay.worker.reconciled_entries == len(qe5.ledger.entries) - 1
