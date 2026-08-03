"""QE6-2 ordinary order/account reconciliation through pinned vn.py."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from enum import Enum, auto
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from src.quant_engine import (
    CnEquityAccount,
    CnEquityFeeSchedule,
    CnEquityOrder,
    EngineIdentity,
    HistoricalRunIdentity,
    QUANTAXIS_ENGINE_VERSION,
    QUANTAXIS_SOURCE_SHA256,
    QuantaxisBacktestEvent,
    QuantaxisBacktestResult,
    QuantaxisBacktestWorkerResult,
    QuantaxisPositionSnapshot,
    QuantaxisRiskAudit,
    QuantaxisRiskPolicy,
    ReconciliationEngineIdentity,
    ReconciliationStore,
    VNPY_ENGINE_COMMIT,
    VNPY_ENGINE_VERSION,
    VNPY_SOURCE_SHA256,
    VnpyOracleAdapter,
    WorkerConfig,
    WorkerRunner,
)
from src.research.contracts import CostSpec
from tests.test_qe6_vnpy_event_path import (
    COMMON_DIR,
    WORKER_DIR,
    _FakeEvent,
    _FakeEventEngine,
    _compilation,
)

for path in (str(COMMON_DIR), str(WORKER_DIR)):
    if path not in os.sys.path:
        os.sys.path.insert(0, path)

from ordinary_replay import build_ordinary_replay_handler  # noqa: E402
from worker_runtime import WorkerError  # noqa: E402


_DAY_1 = date(2025, 1, 2)
_DAY_2 = date(2025, 1, 3)
_SYMBOL = "600001.SH"
_ZERO_COSTS = CostSpec(
    commission_bps=0.0,
    minimum_commission=0.0,
    sell_tax_bps=0.0,
    transfer_fee_bps=0.0,
    slippage_bps=0.0,
    rule_version="qe6-ordinary-zero-v1",
)
_ZERO_FEES = CnEquityFeeSchedule(
    commission_tenths_bps=0,
    minimum_commission_fen=0,
    sell_tax_tenths_bps=0,
    transfer_fee_tenths_bps=0,
    rule_version="qe6-ordinary-zero-v1",
)


def _historical_run(compilation: Any, qe5: QuantaxisBacktestResult) -> HistoricalRunIdentity:
    return HistoricalRunIdentity(
        run_id=f"backtest-record:{'a' * 64}",
        run_content_sha256="a" * 64,
        owner_scope=compilation.engine_request.owner_scope,
        strategy_stream_id="strategy:qe6-test",
        strategy_version_id=f"strategy-version:{'b' * 64}",
        engine_request_sha256=compilation.engine_request.content_sha256,
        execution_plan_sha256=compilation.plan.content_sha256,
        snapshot_sha256=qe5.worker.snapshot_sha256,
        backtest_input_sha256=qe5.worker.backtest_input_sha256,
        ledger_sha256=qe5.ledger.content_sha256,
        original_engine=ReconciliationEngineIdentity(
            name="quantaxis",
            version=qe5.worker.engine_version,
            commit=compilation.engine_request.payload.engine.commit,
            source_sha256=qe5.worker.source_sha256,
        ),
    )


def _evidence(tmp_path: Path, compilation: Any, qe5: QuantaxisBacktestResult) -> dict[str, Any]:
    return {
        "historical_run": _historical_run(compilation, qe5),
        "evidence_store": ReconciliationStore(tmp_path / "reconciliation"),
    }


class _Direction(Enum):
    LONG = auto()
    SHORT = auto()


class _Exchange(Enum):
    SSE = auto()
    SZSE = auto()
    BSE = auto()


class _Status(Enum):
    ALLTRADED = auto()
    REJECTED = auto()


class _Data:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _boundary() -> Mapping[str, Any]:
    return {
        "version": VNPY_ENGINE_VERSION,
        "source_sha256": VNPY_SOURCE_SHA256,
        "Event": _FakeEvent,
        "EventEngine": _FakeEventEngine,
        "Direction": _Direction,
        "Exchange": _Exchange,
        "Status": _Status,
        "OrderData": _Data,
        "TradeData": _Data,
    }


def _qe5_result(compilation: Any, snapshot_sha256: str) -> QuantaxisBacktestResult:
    request = compilation.engine_request.payload
    account = CnEquityAccount(
        ledger_id=f"quantaxis:{request.request_id}",
        data_snapshot_sha256=snapshot_sha256,
        opening_date=_DAY_1,
        initial_cash_fen=100_000,
        rules=_ZERO_FEES,
        board_lot=100,
    )
    buy = CnEquityOrder(
        order_id="buy-1",
        trade_date=_DAY_1,
        symbol=_SYMBOL,
        side="buy",
        requested_shares=100,
        price_fen=100,
    )
    rejected = CnEquityOrder(
        order_id="buy-too-large",
        trade_date=_DAY_2,
        symbol=_SYMBOL,
        side="buy",
        requested_shares=1_000,
        price_fen=100,
    )
    sell = CnEquityOrder(
        order_id="sell-1",
        trade_date=_DAY_2,
        symbol=_SYMBOL,
        side="sell",
        requested_shares=40,
        price_fen=120,
    )
    account.submit_order(buy)
    first_mark = account.mark(
        trade_date=_DAY_1,
        mark_prices_fen={_SYMBOL: 100},
    )
    account.submit_order(rejected)
    account.submit_order(sell)
    second_mark = account.mark(
        trade_date=_DAY_2,
        mark_prices_fen={_SYMBOL: 120},
    )
    events = (
        QuantaxisBacktestEvent(
            event="order",
            trade_date=_DAY_1,
            order=buy,
            market_rule_id="qe6-ordinary-open-v1",
        ),
        QuantaxisBacktestEvent(
            event="mark",
            trade_date=_DAY_1,
            mark_prices_fen={_SYMBOL: 100},
        ),
        QuantaxisBacktestEvent(
            event="order",
            trade_date=_DAY_2,
            order=rejected,
            market_rule_id="qe6-ordinary-open-v1",
        ),
        QuantaxisBacktestEvent(
            event="order",
            trade_date=_DAY_2,
            order=sell,
            market_rule_id="qe6-ordinary-open-v1",
        ),
        QuantaxisBacktestEvent(
            event="mark",
            trade_date=_DAY_2,
            mark_prices_fen={_SYMBOL: 120},
        ),
    )
    worker = QuantaxisBacktestWorkerResult(
        snapshot_sha256=snapshot_sha256,
        engine_request_id=request.request_id,
        execution_plan_sha256=compilation.plan.content_sha256,
        backtest_input_sha256="b" * 64,
        rule_table_version="qe6-ordinary-open-v1",
        fee_schedule=_ZERO_FEES,
        slippage_tenths_bps=0,
        risk_policy=QuantaxisRiskPolicy(),
        opening_date=_DAY_1,
        initial_cash_fen=100_000,
        board_lot=100,
        events=events,
        engine_position_snapshots=(
            QuantaxisPositionSnapshot(
                trade_date=_DAY_1,
                positions=first_mark.positions,
                cash_fen=first_mark.cash_fen,
                dividend_receivable_fen=0,
                equity_fen=first_mark.equity_fen,
            ),
            QuantaxisPositionSnapshot(
                trade_date=_DAY_2,
                positions=second_mark.positions,
                cash_fen=second_mark.cash_fen,
                dividend_receivable_fen=0,
                equity_fen=second_mark.equity_fen,
            ),
        ),
        risk_audit=(
            QuantaxisRiskAudit(
                trade_date=_DAY_1,
                peak_equity_fen=first_mark.equity_fen,
                drawdown_ppm=0,
                cumulative_purchase_notional_fen=10_000,
                purchase_turnover_ppm=100_000,
                drawdown_halted=False,
            ),
            QuantaxisRiskAudit(
                trade_date=_DAY_2,
                peak_equity_fen=second_mark.equity_fen,
                drawdown_ppm=0,
                cumulative_purchase_notional_fen=10_000,
                purchase_turnover_ppm=100_000,
                drawdown_halted=False,
            ),
        ),
        engine_version=QUANTAXIS_ENGINE_VERSION,
        source_sha256=QUANTAXIS_SOURCE_SHA256,
    )
    return QuantaxisBacktestResult(worker=worker, ledger=account.ledger())


class _FakeRunner:
    def __init__(
        self,
        *,
        mutate_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = SimpleNamespace(
            engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT)
        )
        self.mutate_result = mutate_result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        try:
            result = dict(
                build_ordinary_replay_handler(_boundary)(
                    kwargs["payload"],
                    {
                        "path": kwargs["snapshot_path"],
                        "sha256": kwargs["snapshot_sha256"],
                    },
                )
            )
            if self.mutate_result is not None:
                self.mutate_result(result)
            response = {"status": "ok", "error": None, "result": result}
        except WorkerError as exc:
            response = {
                "status": "error",
                "error": {"code": exc.code, "message": str(exc)},
                "result": None,
            }
        return SimpleNamespace(response=response)


def test_qe6_2_reconciles_orders_fills_rejections_and_daily_account(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation = _compilation(
        tmp_path,
        costs=_ZERO_COSTS,
    )
    qe5 = _qe5_result(compilation, snapshot.payload.snapshot_sha256)
    runner = _FakeRunner()

    replay = VnpyOracleAdapter(runner).reconcile_ordinary(  # type: ignore[arg-type]
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
        **_evidence(tmp_path, compilation, qe5),
    )

    assert [item.status for item in replay.worker.orders] == [
        "filled",
        "rejected",
        "filled",
    ]
    assert [item.order_id for item in replay.worker.fills] == ["buy-1", "sell-1"]
    assert replay.worker.rejections[0].reason == "INSUFFICIENT_CASH"
    assert replay.worker.daily_accounts[-1].positions == {_SYMBOL: 60}
    assert replay.worker.daily_accounts[-1].cash_fen == 94_800
    assert replay.worker.daily_accounts[-1].equity_fen == 102_000
    assert runner.calls[0]["operation"] == "ordinary_replay"
    assert replay.artifact.comparison_status == "matched"
    assert replay.decision.status == "validated"
    reopened = ReconciliationStore(tmp_path / "reconciliation")
    assert reopened.get_artifact(
        replay.artifact.artifact_id,
        owner_scope=replay.artifact.owner_scope,
    ) == replay.artifact
    assert reopened.get_decision(
        replay.decision.decision_id,
        owner_scope=replay.decision.owner_scope,
    ) == replay.decision


def test_qe6_2_fails_on_first_ordinary_account_divergence(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation = _compilation(
        tmp_path,
        costs=_ZERO_COSTS,
    )
    qe5 = _qe5_result(compilation, snapshot.payload.snapshot_sha256)

    def mutate(result: dict[str, Any]) -> None:
        result["daily_accounts"][-1]["cash_fen"] += 1
        result["daily_accounts"][-1]["equity_fen"] += 1

    replay = VnpyOracleAdapter(  # type: ignore[arg-type]
        _FakeRunner(mutate_result=mutate)
    ).reconcile_ordinary(
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
        **_evidence(tmp_path, compilation, qe5),
    )
    assert replay.artifact.comparison_status == "diverged"
    assert replay.artifact.first_divergence is not None
    assert replay.artifact.first_divergence.sequence == 5
    assert replay.decision.status == "blocked"


def test_qe6_2_rejects_changed_fill_sequence_even_when_final_state_matches(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation = _compilation(
        tmp_path,
        costs=_ZERO_COSTS,
    )
    qe5 = _qe5_result(compilation, snapshot.payload.snapshot_sha256)

    def mutate(result: dict[str, Any]) -> None:
        result["fills"].reverse()

    replay = VnpyOracleAdapter(  # type: ignore[arg-type]
        _FakeRunner(mutate_result=mutate)
    ).reconcile_ordinary(
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
        **_evidence(tmp_path, compilation, qe5),
    )
    assert replay.artifact.comparison_status == "diverged"
    assert replay.artifact.first_divergence is not None
    assert replay.artifact.first_divergence.sequence == 1
    assert replay.decision.status == "blocked"


def test_qe6_2_rejects_fees_outside_ordinary_scope(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation = _compilation(tmp_path)
    qe5 = _qe5_result(compilation, snapshot.payload.snapshot_sha256)

    with pytest.raises(ValueError, match="excludes fees and slippage"):
        VnpyOracleAdapter(_FakeRunner()).reconcile_ordinary(  # type: ignore[arg-type]
            compilation=compilation,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            qe5=qe5,
            **_evidence(tmp_path, compilation, qe5),
        )


@pytest.mark.integration
def test_qe6_2_real_pinned_vnpy_ordinary_replay(tmp_path: Path) -> None:
    python_text = os.environ.get("VIBE_QE0_VNPY_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_VNPY_PYTHON to run pinned vn.py QE6-2")
    snapshot_path, snapshot, compilation = _compilation(
        tmp_path,
        costs=_ZERO_COSTS,
    )
    qe5 = _qe5_result(compilation, snapshot.payload.snapshot_sha256)
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(WORKER_DIR / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_DIR.resolve(),
    )

    replay = VnpyOracleAdapter(runner).reconcile_ordinary(
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        qe5=qe5,
        **_evidence(tmp_path, compilation, qe5),
    )

    assert replay.worker.engine_version == VNPY_ENGINE_VERSION
    assert replay.worker.source_sha256 == VNPY_SOURCE_SHA256
    assert replay.worker.daily_accounts[-1].equity_fen == 102_000
    assert replay.decision.status == "validated"
