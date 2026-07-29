"""QE4-1 deterministic StrategySpec capability and semantic gates."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.research.contracts import (
    CostSpec,
    DataSnapshotRef,
    EvaluationSpec,
    ExecutionSpec,
    PortfolioSpec,
    RankingSpec,
    RiskSpec,
    SignalRule,
    StrategySpec,
    create_research_object,
)
from src.strategy_spec import (
    STRATEGY_DSL_VERSION,
    StrategySemanticError,
    list_strategy_field_capabilities,
    require_valid_strategy_spec,
    resolve_strategy_field,
    validate_strategy_spec,
)


def _snapshot(*, digest: str = "1" * 64):
    return create_research_object(
        DataSnapshotRef(
            snapshot_sha256=digest,
            as_of=date(2025, 6, 30),
            start_date=date(2024, 1, 2),
            end_date=date(2025, 6, 30),
            adjustment="qfq",
            symbols=("600519.SH", "000858.SZ"),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={
                "600519.SH": "fixture",
                "000858.SZ": "fixture",
            },
        ),
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


def _strategy(*, snapshot=None) -> StrategySpec:
    snapshot = snapshot or _snapshot()
    return StrategySpec(
        data_snapshot_ref=snapshot.ref(),
        title="月度动量 Top 2",
        universe_symbols=("600519.SH", "000858.SZ"),
        signals=(
            SignalRule(
                field="momentum_20d",
                operator="gt",
                value=0.0,
                lookback_days=20,
            ),
        ),
        ranking=RankingSpec(
            field="momentum_20d",
            direction="descending",
            top_n=2,
        ),
        portfolio=PortfolioSpec(
            max_positions=2,
            max_position_weight=0.5,
        ),
        execution=ExecutionSpec(rebalance="monthly"),
        costs=CostSpec(
            commission_bps=3.0,
            minimum_commission=5.0,
            sell_tax_bps=5.0,
            transfer_fee_bps=0.1,
            slippage_bps=5.0,
            rule_version="cn-equity-2025-01-01",
        ),
        risk=RiskSpec(max_drawdown_stop=0.2, max_turnover=12.0),
        evaluation=EvaluationSpec(
            train_end=date(2023, 12, 29),
            validation_end=date(2024, 12, 31),
            test_end=date(2025, 6, 30),
            benchmark="000300.SH",
        ),
    )


def _replace(spec: StrategySpec, **changes) -> StrategySpec:
    raw = spec.model_dump(mode="python")
    raw.update(changes)
    return StrategySpec.model_validate(raw)


def _issue_codes(spec: StrategySpec, *, snapshot=None) -> set[str]:
    return {
        item.code
        for item in validate_strategy_spec(spec, snapshot=snapshot).issues
    }


def test_qe4_1_registry_is_fixed_ordered_and_matches_real_factor_paths() -> None:
    capabilities = list_strategy_field_capabilities()

    assert STRATEGY_DSL_VERSION == "vibe.strategy-spec.v1"
    assert tuple(item.field_id for item in capabilities) == (
        "close",
        "drawdown_20d",
        "momentum_20d",
        "turnover_change_20d",
        "volatility_20d",
    )
    assert resolve_strategy_field("ma_2").lookback_days == 2
    assert resolve_strategy_field("ema_512").source == "quantaxis"
    assert resolve_strategy_field("ma_1") is None
    assert resolve_strategy_field("ema_513") is None
    assert resolve_strategy_field("future_return_20d") is None


def test_qe4_1_valid_strategy_and_snapshot_pass_without_mutation() -> None:
    snapshot = _snapshot()
    spec = _strategy(snapshot=snapshot)

    result = validate_strategy_spec(spec, snapshot=snapshot)

    assert result.valid is True
    assert result.issues == ()
    assert require_valid_strategy_spec(spec, snapshot=snapshot) is spec


def test_nl04_nl07_unknown_future_or_code_fields_fail_closed() -> None:
    spec = _strategy()

    for field in ("future_return_20d", "python_eval", "subprocess_run"):
        invalid = _replace(
            spec,
            signals=(
                SignalRule(
                    field=field,
                    operator="gt",
                    value=0.0,
                    lookback_days=20,
                ),
            ),
        )
        result = validate_strategy_spec(invalid)
        assert result.valid is False
        assert _issue_codes(invalid) == {"unknown_field"}
        with pytest.raises(StrategySemanticError, match="unknown_field"):
            require_valid_strategy_spec(invalid, snapshot=_snapshot())


def test_qe4_1_operator_value_and_lookback_semantics_are_bounded() -> None:
    spec = _strategy()
    invalid = _replace(
        spec,
        signals=(
            SignalRule(
                field="momentum_20d",
                operator="crosses_above",
                value="unknown_factor",
                lookback_days=60,
                consecutive_days=2,
            ),
            SignalRule(
                field="volatility_20d",
                operator="lt",
                value="低波动",
                lookback_days=20,
            ),
        ),
    )

    assert _issue_codes(invalid) == {
        "crossing_consecutive_days",
        "invalid_field_reference",
        "invalid_value",
        "lookback_mismatch",
        "unsupported_operator",
    }


def test_qe4_1_boolean_threshold_is_not_coerced_to_a_number() -> None:
    with pytest.raises(ValidationError, match="valid number|valid integer|valid string"):
        SignalRule(
            field="momentum_20d",
            operator="gt",
            value=True,
            lookback_days=20,
        )


def test_qe4_1_crossing_references_must_be_allowlisted_and_not_self() -> None:
    spec = _strategy()
    valid_cross = _replace(
        spec,
        signals=(
            SignalRule(
                field="close",
                operator="crosses_above",
                value="ma_20",
                lookback_days=1,
            ),
        ),
    )
    assert validate_strategy_spec(valid_cross).valid is True

    self_cross = _replace(
        spec,
        signals=(
            SignalRule(
                field="close",
                operator="crosses_below",
                value="close",
                lookback_days=1,
            ),
        ),
    )
    assert _issue_codes(self_cross) == {"field_self_reference"}


def test_qe4_1_duplicate_ranking_and_allocation_conflicts_fail_closed() -> None:
    spec = _strategy()
    signal = spec.signals[0]
    invalid = _replace(
        spec,
        signals=(signal, signal),
        ranking=RankingSpec(
            field="not_a_factor",
            direction="descending",
            top_n=1,
        ),
        portfolio=PortfolioSpec(
            max_positions=2,
            max_position_weight=0.2,
            cash_buffer_weight=0.1,
        ),
    )

    assert _issue_codes(invalid) == {
        "allocation_infeasible",
        "duplicate_signal",
        "positions_exceed_ranking",
        "unknown_field",
    }


def test_qe4_1_a_share_execution_and_universe_limits_fail_closed() -> None:
    spec = _strategy()
    invalid = _replace(
        spec,
        portfolio=PortfolioSpec(
            max_positions=3,
            max_position_weight=0.5,
        ),
        execution=ExecutionSpec(
            rebalance="monthly",
            enforce_t_plus_one=False,
            board_lot=1,
        ),
    )

    assert _issue_codes(invalid) == {
        "a_share_rule_mismatch",
        "positions_exceed_ranking",
        "positions_exceed_universe",
    }


def test_qe4_1_strategy_universe_rejects_noncanonical_symbols() -> None:
    spec = _strategy()
    raw = spec.model_dump(mode="python")
    raw["universe_symbols"] = ("600519.SH", "../escape")

    with pytest.raises(ValidationError, match="invalid canonical identifier"):
        StrategySpec.model_validate(raw)


def test_qe4_1_snapshot_identity_symbols_and_as_of_are_bound() -> None:
    expected = _snapshot()
    supplied = _snapshot(digest="2" * 64)
    base = _strategy(snapshot=expected)
    invalid = _replace(
        base,
        universe_symbols=("600519.SH", "000001.SZ"),
        evaluation=EvaluationSpec(
            train_end=date(2023, 12, 29),
            validation_end=date(2024, 12, 31),
            test_end=date(2025, 7, 1),
            benchmark="000300.SH",
        ),
    )

    assert _issue_codes(invalid, snapshot=supplied) == {
        "evaluation_after_snapshot",
        "snapshot_ref_mismatch",
        "symbol_outside_snapshot",
    }


def test_ar06_contract_forbids_same_close_fill_and_zero_lag() -> None:
    with pytest.raises(ValidationError, match="next_open|next_vwap"):
        ExecutionSpec.model_validate(
            {
                "signal_price": "close",
                "fill_price": "close",
                "signal_lag_bars": 1,
                "rebalance": "monthly",
            }
        )

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ExecutionSpec(
            fill_price="next_open",
            signal_lag_bars=0,
            rebalance="monthly",
        )
