"""QE5-2 confirmed-plan QUANTAXIS worker bridge and oracle reconciliation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from src.quant_engine import (
    EngineIdentity,
    QUANTAXIS_ENGINE_COMMIT,
    QUANTAXIS_SOURCE_SHA256,
    QuantaxisAdapter,
    QuantaxisReconciliationError,
    WorkerConfig,
    WorkerRunner,
    compute_snapshot_sha256,
    write_quantaxis_backtest_snapshot,
)
from src.research.contracts import (
    DataSnapshotRef,
    EngineIdentitySpec,
    ResearchSpec,
    ResourceLimits,
    create_research_object,
)
from src.strategy_spec import (
    StrategyDraftRequest,
    StrategyTemplateSource,
    StrategyVersionStore,
    compile_strategy_template,
    draft_strategy_from_language,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = AGENT_ROOT / "engine_workers" / "quantaxis"
COMMON_DIR = AGENT_ROOT / "engine_workers" / "common"
for path in (str(COMMON_DIR), str(WORKER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from formal_backtest import build_backtest_handler  # noqa: E402
from worker_runtime import WorkerError  # noqa: E402


_T0 = datetime(2026, 7, 31, 3, 0, tzinfo=timezone.utc)
_SYMBOLS = ("000002.SZ", "600001.SH")


class _DraftModel:
    def __init__(
        self,
        ranking_field: str = "momentum_20d",
        *,
        max_drawdown_stop: float | None = None,
        max_turnover: float | None = None,
    ) -> None:
        self.ranking_field = ranking_field
        self.max_drawdown_stop = max_drawdown_stop
        self.max_turnover = max_turnover

    def generate(self, _messages) -> str:
        payload = {
            "schema_version": "vibe.strategy-draft.v1",
            "template_id": "top_n_rebalance",
            "title": "QE5-2 日频动量",
            "ranking_field": self.ranking_field,
            "ranking_direction": "descending",
            "top_n": 1,
            "rebalance": "daily",
            "max_positions": 1,
            "max_position_weight": 0.99,
            "cash_buffer_weight": 0.01,
            "train_end": "2025-01-02",
            "validation_end": "2025-01-03",
            "test_end": "2025-01-07",
        }
        if self.max_drawdown_stop is not None:
            payload["max_drawdown_stop"] = self.max_drawdown_stop
        if self.max_turnover is not None:
            payload["max_turnover"] = self.max_turnover
        return json.dumps(payload)


def _snapshot_payload() -> dict:
    bars = []
    momentum = {
        "2025-01-02": {"000002.SZ": 0.1, "600001.SH": 0.9},
        "2025-01-03": {"000002.SZ": 0.2, "600001.SH": 0.8},
        "2025-01-06": {"000002.SZ": 0.95, "600001.SH": 0.1},
        "2025-01-07": {"000002.SZ": 0.9, "600001.SH": 0.2},
    }
    prices = {
        "000002.SZ": (2000, 2010, 1990, 2005),
        "600001.SH": (1000, 1010, 990, 1005),
    }
    for trade_date in momentum:
        for symbol in _SYMBOLS:
            open_fen, high_fen, low_fen, close_fen = prices[symbol]
            bars.append(
                {
                    "trade_date": trade_date,
                    "known_at": f"{trade_date}T15:01:00+08:00",
                    "symbol": symbol,
                    "open_fen": open_fen,
                    "high_fen": high_fen,
                    "low_fen": low_fen,
                    "close_fen": close_fen,
                    "signal_close_fen": close_fen,
                    "limit_reference_fen": close_fen,
                    "volume_shares": 1_000_000,
                    "status": "traded",
                    "is_st": False,
                    "listing_trade_day_number": 5_000,
                    "market_rule_id": (
                        "sz-main-10pct-v1"
                        if symbol.endswith(".SZ")
                        else "sh-main-10pct-v1"
                    ),
                    "features": {"momentum_20d": momentum[trade_date][symbol]},
                }
            )
    return {
        "schema_version": "vibe.quantaxis-backtest-snapshot.v1",
        "price_semantics": {
            "execution_price_adjustment": "raw",
            "signal_price_adjustment": "qfq",
            "corporate_action_mode": "explicit",
        },
        "rule_table": {
            "schema_version": "vibe.cn-equity-rule-table.v1",
            "version": "cn-equity-rules-2025-v1",
            "fee_schedule": {
                "effective_from": "2025-01-01",
                "effective_to": "2025-12-31",
                "commission_tenths_bps": 30,
                "minimum_commission_fen": 500,
                "sell_tax_tenths_bps": 50,
                "transfer_fee_tenths_bps": 1,
                "rule_version": "cn-equity-2025-01-01",
            },
            "market_rules": [
                {
                    "rule_id": "sz-main-10pct-v1",
                    "effective_from": "2020-01-01",
                    "effective_to": "2030-12-31",
                    "board": "sz_main",
                    "is_st": False,
                    "listing_day_min": 1,
                    "listing_day_max": None,
                    "limit_up_bps": 1000,
                    "limit_down_bps": 1000,
                    "tick_fen": 1,
                },
                {
                    "rule_id": "sh-main-10pct-v1",
                    "effective_from": "2020-01-01",
                    "effective_to": "2030-12-31",
                    "board": "sh_main",
                    "is_st": False,
                    "listing_day_min": 1,
                    "listing_day_max": None,
                    "limit_up_bps": 1000,
                    "limit_down_bps": 1000,
                    "tick_fen": 1,
                },
            ],
            "max_participation_bps": 1000,
        },
        "instruments": [
            {
                "symbol": "000002.SZ",
                "board": "sz_main",
                "listing_date": "2000-01-01",
                "delisting_date": None,
            },
            {
                "symbol": "600001.SH",
                "board": "sh_main",
                "listing_date": "2000-01-01",
                "delisting_date": None,
            },
        ],
        "calendar": [
            {"trade_date": item, "is_open": True}
            for item in ("2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07")
        ],
        "bars": bars,
        "corporate_actions": [
            {
                "action_id": "dividend-600001-20250106",
                "symbol": "600001.SH",
                "kind": "cash_dividend",
                "known_at": "2025-01-02T09:00:00+08:00",
                "record_date": "2025-01-03",
                "ex_date": "2025-01-06",
                "pay_date": "2025-01-06",
                "multiplier_numerator": 1,
                "multiplier_denominator": 1,
                "cash_per_share_fen": 10,
            }
        ],
    }


class _Indicators:
    @staticmethod
    def QA_indicator_MA(frame, window: int):
        import pandas as pd

        return pd.DataFrame(
            {f"MA{window}": frame["close"].rolling(window).mean()},
            index=frame.index,
        )

    @staticmethod
    def QA_indicator_EMA(frame, window: int):
        import pandas as pd

        return pd.DataFrame(
            {"EMA": frame["close"].ewm(span=window, adjust=False).mean()},
            index=frame.index,
        )


class _FakeQifiAccount:
    def __init__(self, *_args, **_kwargs) -> None:
        self.orders = []

    def create_backtestaccount(self) -> None:
        return None

    def send_order(self, *args, **kwargs):
        order = {"args": args, "kwargs": kwargs}
        self.orders.append(order)
        return order

    def make_deal(self, _order) -> None:
        return None


class _FakeQifi:
    QIFI_Account = _FakeQifiAccount


class _Directions:
    BUY = "BUY"
    SELL = "SELL"


class _Parameters:
    ORDER_DIRECTION = _Directions


def _load_boundary() -> dict:
    return {
        "version": "2.1.0a2",
        "source_sha256": QUANTAXIS_SOURCE_SHA256,
        "indicators": _Indicators(),
        "qifi": _FakeQifi(),
        "parameters": _Parameters(),
    }


class _WorkerRunner:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT)
        )
        self.calls: list[dict] = []
        self.handler = build_backtest_handler(_load_boundary)

    def run(self, **kwargs):
        self.calls.append(kwargs)
        try:
            result = self.handler(
                kwargs["payload"],
                {
                    "path": kwargs["snapshot_path"],
                    "sha256": kwargs["snapshot_sha256"],
                },
            )
            response = {"status": "ok", "error": None, "result": result}
        except WorkerError as exc:
            response = {
                "status": "error",
                "error": {"code": exc.code, "message": str(exc)},
                "result": None,
            }
        return SimpleNamespace(response=response)


def _confirmed_chain(
    tmp_path: Path,
    *,
    draft_model: _DraftModel | None = None,
    snapshot_payload: dict | None = None,
):
    snapshot_path = tmp_path / "qe5-backtest-snapshot.json"
    snapshot_sha256 = write_quantaxis_backtest_snapshot(
        snapshot_payload or _snapshot_payload(),
        snapshot_path,
    )
    research = create_research_object(
        ResearchSpec(
            symbols=("600001.SH",),
            as_of=date(2025, 1, 7),
            lookback_days=(20,),
            candidate_universe="qe5-worker-fixture",
            requested_outputs=("strategy", "backtest"),
        ),
        created_at=_T0,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256=snapshot_sha256,
            as_of=date(2025, 1, 7),
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 7),
            adjustment="qfq",
            symbols=_SYMBOLS,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("qe5-fixture",),
            actual_sources={symbol: "qe5-fixture" for symbol in _SYMBOLS},
        ),
        parent_refs=(research.ref(),),
        created_at=_T0,
    )
    source = StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=_SYMBOLS,
    )
    draft = draft_strategy_from_language(
        draft_model or _DraftModel(),
        StrategyDraftRequest(user_text="每天选择动量最高的一只"),
        source=source,
        created_at=_T0,
    )
    assert draft.status == "ready" and draft.build is not None
    compilation = compile_strategy_template(
        draft.build,
        snapshot=snapshot,
        engine=EngineIdentitySpec(
            name="quantaxis",
            commit=QUANTAXIS_ENGINE_COMMIT,
        ),
        resource_limits=ResourceLimits(
            timeout_seconds=30,
            max_stdout_bytes=2_000_000,
            max_stderr_bytes=1_000_000,
            memory_bytes=1_073_741_824,
        ),
        random_seed=7,
        created_at=_T0,
    )
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, draft_head = store.create_initial_version(
            stream_id="qe5-session",
            owner_scope="household:v1",
            result=draft,
            created_at=_T0,
        )
        card, awaiting = store.prepare_confirmation(
            stream_id="qe5-session",
            expected_head=draft_head,
            issued_at=_T0 + timedelta(minutes=1),
            expires_at=_T0 + timedelta(minutes=11),
        )
        receipt = store.confirm(
            stream_id="qe5-session",
            expected_head=awaiting,
            confirmation_hash=card.confirmation_hash,
            idempotency_key="qe5-confirm-1",
            actor_id="household-user",
            confirmed_at=_T0 + timedelta(minutes=2),
        )
        head = store.get_head("qe5-session")
        assert head is not None
    return snapshot_path, snapshot, compilation, version, head, card, receipt


def test_qe5_2_snapshot_writer_is_canonical_idempotent_and_no_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshot.json"
    payload = _snapshot_payload()
    first = write_quantaxis_backtest_snapshot(payload, path)
    second = write_quantaxis_backtest_snapshot(
        dict(reversed(tuple(payload.items()))),
        path,
    )

    assert first == second == compute_snapshot_sha256(path)
    assert path.stat().st_mode & 0o777 == 0o600
    changed = _snapshot_payload()
    changed["bars"][0]["close_fen"] += 1
    with pytest.raises(ValueError, match="different content"):
        write_quantaxis_backtest_snapshot(changed, path)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        write_quantaxis_backtest_snapshot(payload, linked / "snapshot.json")


def test_qe5_2_confirmed_plan_runs_worker_and_reconciles_daily_oracle(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    runner = _WorkerRunner()
    result = QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    assert result.worker.rule_table_version == "cn-equity-rules-2025-v1"
    assert result.worker.slippage_tenths_bps == 50
    assert result.worker.risk_policy.max_drawdown_ppm == 200_000
    assert result.worker.risk_policy.max_purchase_turnover_ppm == 12_000_000
    assert result.worker.engine_request_id == compilation.engine_request.payload.request_id
    assert len(result.worker.engine_position_snapshots) == 4
    assert result.worker.signal_audit[0]["selected_symbols"] == ["600001.SH"]
    assert result.worker.signal_audit[2]["selected_symbols"] == ["000002.SZ"]
    assert result.worker.signal_audit[0]["evaluated_symbol_count"] == 2
    assert result.worker.signal_audit[0]["eligible_symbol_count"] == 1
    assert result.worker.signal_audit[0]["rules_truncated"] is True
    assert len(result.worker.signal_audit[0]["rule_outcomes_sha256"]) == 64
    assert set(result.worker.signal_audit[0]["rules"]) == {"600001.SH"}
    assert any(entry.event == "dividend_ex" for entry in result.ledger.entries)
    assert any(entry.event == "dividend_pay" for entry in result.ledger.entries)
    assert result.ledger.entries[-1].positions == {"000002.SZ": 400}
    assert result.ledger.data_snapshot_sha256 == compute_snapshot_sha256(snapshot_path)
    assert runner.calls[0]["operation"] == "backtest"
    assert runner.calls[0]["timeout_seconds"] == 30
    assert runner.calls[0]["memory_bytes"] == 1_073_741_824
    assert runner.calls[0]["max_open_files"] == 256
    order_dates = [
        event.trade_date
        for event in result.worker.events
        if event.event == "order"
    ]
    assert order_dates[0] == date(2025, 1, 3)

    replay = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )
    assert replay.worker == result.worker
    assert replay.ledger.content_sha256 == result.ledger.content_sha256


def test_qe5_2_rational_cash_dividend_reconciles_exactly(
    tmp_path: Path,
) -> None:
    payload = _snapshot_payload()
    action = payload["corporate_actions"][0]
    del action["cash_per_share_fen"]
    action["cash_per_share_numerator_fen"] = 2_700_039
    action["cash_per_share_denominator"] = 5_000
    action["cash_rounding"] = "half_up_total_fen"
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path, snapshot_payload=payload)
    )

    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )
    event = next(
        item for item in result.worker.events if item.event == "dividend_ex"
    )
    assert event.cash_per_share_fen is None
    assert event.cash_per_share_numerator_fen == 2_700_039
    assert event.cash_per_share_denominator == 5_000
    assert event.cash_rounding == "half_up_total_fen"
    ledger_entry = next(
        item for item in result.ledger.entries if item.event == "dividend_ex"
    )
    numerator = event.entitled_shares * 2_700_039
    quotient, remainder = divmod(numerator, 5_000)
    assert ledger_entry.dividend_receivable_delta_fen == (
        quotient + int(remainder * 2 >= 5_000)
    )


def test_qe5_2_uses_pinned_quantaxis_factor_for_strategy_ranking(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path, draft_model=_DraftModel("ma_2"))
    )
    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    assert result.worker.signal_audit[0]["selected_symbols"] == []
    assert result.worker.signal_audit[1]["selected_symbols"] == ["000002.SZ"]
    assert compilation.plan.field_bindings[-1].source == "quantaxis"


def test_qe5_2_confirmed_purchase_turnover_caps_worker_fills(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(
            tmp_path,
            draft_model=_DraftModel(max_turnover=0.11),
        )
    )
    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    buys = [
        entry
        for entry in result.ledger.entries
        if entry.event == "buy"
    ]
    assert [entry.filled_shares for entry in buys] == [100]
    assert result.worker.risk_policy.max_purchase_turnover_ppm == 110_000
    assert result.worker.risk_audit[-1].purchase_turnover_ppm == 100_100


def test_qe5_2_confirmed_drawdown_stop_liquidates_and_blocks_reentry(
    tmp_path: Path,
) -> None:
    payload = _snapshot_payload()
    falling = next(
        item
        for item in payload["bars"]
        if item["trade_date"] == "2025-01-06" and item["symbol"] == "600001.SH"
    )
    falling.update(
        {
            "high_fen": 1_000,
            "low_fen": 500,
            "close_fen": 500,
            "signal_close_fen": 500,
        }
    )
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(
            tmp_path,
            draft_model=_DraftModel(max_drawdown_stop=0.01),
            snapshot_payload=payload,
        )
    )
    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    assert result.worker.risk_audit[2].drawdown_halted is True
    assert result.ledger.entries[-1].positions == {}
    assert not any(
        entry.event == "buy" and entry.trade_date == date(2025, 1, 7)
        for entry in result.ledger.entries
    )


def test_qe5_2_share_split_reconciles_exact_lots_and_raw_accounting(
    tmp_path: Path,
) -> None:
    payload = _snapshot_payload()
    payload["corporate_actions"] = [
        {
            "action_id": "split-600001-20250106",
            "symbol": "600001.SH",
            "kind": "share_split",
            "known_at": "2025-01-02T09:00:00+08:00",
            "record_date": "2025-01-03",
            "ex_date": "2025-01-06",
            "pay_date": "2025-01-06",
            "multiplier_numerator": 2,
            "multiplier_denominator": 1,
            "cash_per_share_fen": 0,
        }
    ]
    for bar in payload["bars"]:
        if bar["symbol"] != "600001.SH" or bar["trade_date"] < "2025-01-06":
            continue
        bar.update(
            {
                "open_fen": 500,
                "high_fen": 510,
                "low_fen": 490,
                "close_fen": 505,
                "signal_close_fen": 1_005,
                "limit_reference_fen": 505,
            }
        )
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path, snapshot_payload=payload)
    )
    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    split = next(
        entry for entry in result.ledger.entries if entry.event == "share_split"
    )
    assert split.position_delta == {"600001.SH": 900}
    assert split.positions["600001.SH"] == 1_800
    assert result.ledger.entries[-1].positions == {"000002.SZ": 400}


def test_qe5_2_unconfirmed_head_never_starts_worker(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    runner = _WorkerRunner()
    unconfirmed = head.model_copy(update={"state": "awaiting_confirmation"})
    with pytest.raises(ValueError, match="must be confirmed"):
        QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
            compilation=compilation,
            version=version,
            head=unconfirmed,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            initial_cash_fen=1_000_000,
        )
    assert runner.calls == []


def test_qe5_2_confirmation_receipt_must_match_exact_card(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    runner = _WorkerRunner()
    mismatched = receipt.model_copy(update={"confirmation_hash": "0" * 64})
    with pytest.raises(ValueError, match="exact card"):
        QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
            compilation=compilation,
            version=version,
            head=head,
            card=card,
            receipt=mismatched,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            initial_cash_fen=1_000_000,
        )
    assert runner.calls == []


def test_qe5_2_snapshot_bytes_and_data_ref_must_match(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["bars"][0]["close_fen"] += 1
    snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    runner = _WorkerRunner()
    with pytest.raises(ValueError, match="does not bind"):
        QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
            compilation=compilation,
            version=version,
            head=head,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            initial_cash_fen=1_000_000,
        )
    assert runner.calls == []


def test_qe5_2_oracle_divergence_fails_closed(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    runner = _WorkerRunner()
    original_handler = runner.handler

    def divergent(payload, worker_snapshot):
        result = dict(original_handler(payload, worker_snapshot))
        snapshots = [dict(item) for item in result["engine_position_snapshots"]]
        snapshots[-1]["cash_fen"] += 1
        result["engine_position_snapshots"] = snapshots
        return result

    runner.handler = divergent
    with pytest.raises(QuantaxisReconciliationError, match="diverged"):
        QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
            compilation=compilation,
            version=version,
            head=head,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            initial_cash_fen=1_000_000,
        )


def test_qe5_2_worker_provenance_mismatch_fails_closed(tmp_path: Path) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    runner = _WorkerRunner()
    original_handler = runner.handler

    def wrong_provenance(payload, worker_snapshot):
        result = dict(original_handler(payload, worker_snapshot))
        result["source_sha256"] = {
            **result["source_sha256"],
            "qifi_account": "0" * 64,
        }
        return result

    runner.handler = wrong_provenance
    with pytest.raises(ValueError, match="engine provenance"):
        QuantaxisAdapter(runner).backtest(  # type: ignore[arg-type]
            compilation=compilation,
            version=version,
            head=head,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            initial_cash_fen=1_000_000,
        )


def test_qe5_2_historical_market_rule_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["bars"][0]["market_rule_id"] = "sh-main-10pct-v1"
    snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    # Rebind the outer data reference so the worker, not the main hash gate,
    # proves that dated board/rule mismatches fail closed.
    new_sha = compute_snapshot_sha256(snapshot_path)
    worker_payload = {
        "schema_version": "vibe.quantaxis-backtest-request.v1",
        "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": {
            **snapshot.payload.model_dump(mode="json"),
            "snapshot_sha256": new_sha,
        },
        "confirmation": {
            "version_id": version.version_id,
            "card_id": card.card_id,
            "receipt_id": receipt.receipt_id,
            "confirmation_hash": receipt.confirmation_hash,
        },
        "initial_cash_fen": 1_000_000,
    }
    handler = build_backtest_handler(_load_boundary)
    with pytest.raises(WorkerError) as raised:
        handler(worker_payload, {"path": str(snapshot_path), "sha256": new_sha})
    assert raised.value.code == "RULE_BINDING_ERROR"


def test_qe5_2_ipo_rule_uses_explicit_listing_trade_day_not_code_prefix(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, _head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["bars"][0]["listing_trade_day_number"] = 1
    referenced_rule = next(
        item
        for item in payload["rule_table"]["market_rules"]
        if item["rule_id"] == payload["bars"][0]["market_rule_id"]
    )
    referenced_rule["listing_day_min"] = 6
    snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    new_sha = compute_snapshot_sha256(snapshot_path)
    worker_payload = {
        "schema_version": "vibe.quantaxis-backtest-request.v1",
        "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": {
            **snapshot.payload.model_dump(mode="json"),
            "snapshot_sha256": new_sha,
        },
        "confirmation": {
            "version_id": version.version_id,
            "card_id": card.card_id,
            "receipt_id": receipt.receipt_id,
            "confirmation_hash": receipt.confirmation_hash,
        },
        "initial_cash_fen": 1_000_000,
    }
    with pytest.raises(WorkerError) as raised:
        build_backtest_handler(_load_boundary)(
            worker_payload,
            {"path": str(snapshot_path), "sha256": new_sha},
        )
    assert raised.value.code == "RULE_BINDING_ERROR"


def test_qe5_2_price_semantics_must_match_data_snapshot_ref(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, _head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["price_semantics"]["signal_price_adjustment"] = "hfq"
    snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    new_sha = compute_snapshot_sha256(snapshot_path)
    worker_payload = {
        "schema_version": "vibe.quantaxis-backtest-request.v1",
        "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": {
            **snapshot.payload.model_dump(mode="json"),
            "snapshot_sha256": new_sha,
        },
        "confirmation": {
            "version_id": version.version_id,
            "card_id": card.card_id,
            "receipt_id": receipt.receipt_id,
            "confirmation_hash": receipt.confirmation_hash,
        },
        "initial_cash_fen": 1_000_000,
    }
    with pytest.raises(WorkerError) as raised:
        build_backtest_handler(_load_boundary)(
            worker_payload,
            {"path": str(snapshot_path), "sha256": new_sha},
        )
    assert raised.value.code == "SNAPSHOT_SEMANTICS_MISMATCH"


def test_qe5_2_bar_known_after_trade_date_fails_point_in_time(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation, version, _head, card, receipt = (
        _confirmed_chain(tmp_path)
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["bars"][0]["known_at"] = "2025-01-03T09:00:00+08:00"
    snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    new_sha = compute_snapshot_sha256(snapshot_path)
    worker_payload = {
        "schema_version": "vibe.quantaxis-backtest-request.v1",
        "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": {
            **snapshot.payload.model_dump(mode="json"),
            "snapshot_sha256": new_sha,
        },
        "confirmation": {
            "version_id": version.version_id,
            "card_id": card.card_id,
            "receipt_id": receipt.receipt_id,
            "confirmation_hash": receipt.confirmation_hash,
        },
        "initial_cash_fen": 1_000_000,
    }
    with pytest.raises(WorkerError) as raised:
        build_backtest_handler(_load_boundary)(
            worker_payload,
            {"path": str(snapshot_path), "sha256": new_sha},
        )
    assert raised.value.code == "POINT_IN_TIME_VIOLATION"


def test_qe5_2_worker_capability_is_registered() -> None:
    worker_source = (WORKER_DIR / "worker.py").read_text(encoding="utf-8")
    assert '"backtest": "qe5"' in worker_source
    assert "_load_quantaxis_backtest_boundary" in worker_source
    assert '"backtest": build_backtest_handler(' in worker_source


@pytest.mark.integration
def test_qe5_2_real_pinned_quantaxis_worker_is_offline_and_replayable(
    tmp_path: Path,
) -> None:
    python_text = os.environ.get("VIBE_QE0_QUANTAXIS_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_QUANTAXIS_PYTHON to run pinned QE5 backtest")
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path, draft_model=_DraftModel("ma_2"))
    )
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(WORKER_DIR / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_DIR.resolve(),
    )
    first = QuantaxisAdapter(runner).backtest(
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )
    second = QuantaxisAdapter(runner).backtest(
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    assert first.worker.engine_version == "2.1.0a2"
    assert set(first.worker.source_sha256) == {
        "data_fq",
        "indicator_base",
        "indicators",
        "calendar",
        "parameters",
    }
    assert first.worker.source_sha256["indicators"] == (
        "94995068dbe73fdeddfea69bd51c0df52af6fc4567c5c432a265c2e3f9363ff5"
    )
    assert first.worker == second.worker
    assert first.ledger.content_sha256 == second.ledger.content_sha256


@pytest.mark.integration
def test_qe5_2_real_pinned_worker_reconciles_rational_cash_dividend(
    tmp_path: Path,
) -> None:
    python_text = os.environ.get("VIBE_QE0_QUANTAXIS_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_QUANTAXIS_PYTHON to run pinned QE5 backtest")
    payload = _snapshot_payload()
    action = payload["corporate_actions"][0]
    del action["cash_per_share_fen"]
    action.update(
        {
            "cash_per_share_numerator_fen": 2_700_039,
            "cash_per_share_denominator": 5_000,
            "cash_rounding": "half_up_total_fen",
        }
    )
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
            _confirmed_chain(
                tmp_path,
                snapshot_payload=payload,
                draft_model=_DraftModel(),
            )
    )
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(WORKER_DIR / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_DIR.resolve(),
    )
    result = QuantaxisAdapter(runner).backtest(
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=1_000_000,
    )

    event = next(
        item for item in result.worker.events if item.event == "dividend_ex"
    )
    assert event.cash_per_share_numerator_fen == 2_700_039
    assert event.cash_per_share_denominator == 5_000
    assert event.cash_rounding == "half_up_total_fen"
    assert any(item.event == "dividend_pay" for item in result.ledger.entries)
