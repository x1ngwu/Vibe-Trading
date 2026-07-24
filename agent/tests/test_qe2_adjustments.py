"""QE2 formula and historical-rule tests; all inputs are offline."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from backtest.loaders.a_share_rules import AShareRuleError, resolve_a_share_rule
from backtest.loaders.adjustments import (
    AdjustmentContext,
    AdjustmentError,
    AdjustmentFactorPoint,
    adjust_stock_frame,
)


def _raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [20.0, 19.5, 10.0],
            "high": [20.2, 19.8, 10.2],
            "low": [19.8, 19.4, 9.8],
            "close": [20.0, 19.7, 10.1],
            "volume": [80_000.0, 90_000.0, 190_000.0],
            "amount": [1_600_000.0, 1_764_000.0, 1_919_000.0],
        },
        index=pd.DatetimeIndex(
            ["2025-01-03", "2025-01-06", "2025-01-07"], name="trade_date"
        ),
    )


def _context() -> AdjustmentContext:
    # Cash event factor is 1 / 0.975; the later 2-for-1 event doubles both
    # cumulative price and share factors.
    return AdjustmentContext(
        symbol="600002.SH",
        source="frozen-fixture",
        version="cash-plus-share-v1",
        as_of="2025-01-07",
        points=(
            AdjustmentFactorPoint(
                trade_date="2025-01-03", price_factor=1.0, share_factor=1.0,
                known_at="2025-01-03T15:01:00+08:00",
            ),
            AdjustmentFactorPoint(
                trade_date="2025-01-06", price_factor=1 / 0.975, share_factor=1.0,
                known_at="2025-01-06T15:01:00+08:00",
            ),
            AdjustmentFactorPoint(
                trade_date="2025-01-07", price_factor=2 / 0.975, share_factor=2.0,
                known_at="2025-01-07T15:01:00+08:00",
            ),
        ),
    )


def test_dt08_raw_qfq_hfq_use_explicit_anchors_and_preserve_amount() -> None:
    raw = _raw_frame()
    context = _context()

    raw_result = adjust_stock_frame(raw, adjustment="raw", context=context)
    qfq = adjust_stock_frame(raw, adjustment="qfq", context=context)
    hfq = adjust_stock_frame(raw, adjustment="hfq", context=context)

    pd.testing.assert_frame_equal(raw_result.frame, raw)
    assert qfq.frame.iloc[-1]["close"] == pytest.approx(raw.iloc[-1]["close"])
    assert hfq.frame.iloc[0]["close"] == pytest.approx(raw.iloc[0]["close"])
    assert qfq.frame.iloc[0]["close"] == pytest.approx(20.0 * 0.975 / 2)
    assert hfq.frame.iloc[-1]["close"] == pytest.approx(10.1 * 2 / 0.975)

    # Share changes rescale volume; the cash-only step does not. Amount never changes.
    assert qfq.frame.iloc[0]["volume"] == pytest.approx(160_000.0)
    assert qfq.frame.iloc[1]["volume"] == pytest.approx(180_000.0)
    assert qfq.frame.iloc[2]["volume"] == pytest.approx(190_000.0)
    assert hfq.frame.iloc[2]["volume"] == pytest.approx(95_000.0)
    pd.testing.assert_series_equal(qfq.frame["amount"], raw["amount"])
    pd.testing.assert_series_equal(hfq.frame["amount"], raw["amount"])

    assert qfq.manifest.price_anchor_date == date(2025, 1, 7)
    assert hfq.manifest.price_anchor_date == date(2025, 1, 3)
    assert qfq.manifest.factor_context_sha256 == context.context_sha256
    assert qfq.manifest.input_sha256 == hfq.manifest.input_sha256
    assert qfq.manifest.output_sha256 != hfq.manifest.output_sha256


def test_dt05_factor_context_fails_closed_on_future_or_missing_facts() -> None:
    with pytest.raises(ValidationError, match="must be known by as_of"):
        AdjustmentContext(
            symbol="600002.SH", source="fixture", version="future-v1",
            as_of="2025-01-07",
            points=(
                AdjustmentFactorPoint(
                    trade_date="2025-01-03", price_factor=1.0, share_factor=1.0,
                    known_at="2025-01-08T09:00:00+08:00",
                ),
            ),
        )

    incomplete = _context().model_copy(update={"points": _context().points[:-1]})
    with pytest.raises(AdjustmentError, match=r"2025-01-07"):
        adjust_stock_frame(_raw_frame(), adjustment="qfq", context=incomplete)


def test_dt08_historical_rule_boundary_is_effective_dated_and_fail_closed() -> None:
    before = resolve_a_share_rule(
        board="chinext", is_st=False, trade_date=date(2020, 8, 23)
    )
    after = resolve_a_share_rule(
        board="chinext", is_st=False, trade_date=date(2020, 8, 24)
    )
    st = resolve_a_share_rule(
        board="sh_main", is_st=True, trade_date=date(2025, 1, 6)
    )

    assert before.rule_id == "chinext-normal-v1"
    assert before.price_limit_rate == pytest.approx(0.10)
    assert after.rule_id == "chinext-normal-v2"
    assert after.price_limit_rate == pytest.approx(0.20)
    assert after.no_limit_initial_trading_days == 5
    assert st.price_limit_rate == pytest.approx(0.05)
    assert st.buy_lot_shares == 100
    assert st.sell_settlement_days == 1
    assert st.source

    with pytest.raises(AShareRuleError, match="found=0"):
        resolve_a_share_rule(board="chinext", is_st=True, trade_date=date(2025, 1, 6))


def test_dt08_two_frozen_real_action_windows_match_official_ex_price_formula() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "research"
        / "qe2_real_adjustment_windows_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == "vibe.real-adjustment-windows.v1"
    assert {item["kind"] for item in fixture["windows"]} == {
        "cash_dividend",
        "cash_dividend_and_capitalization",
    }
    for window in fixture["windows"]:
        bars = window["bars"]
        record = next(item for item in bars if item["trade_date"] == window["record_date"])
        ex_bar = next(item for item in bars if item["trade_date"] == window["ex_date"])
        theoretical_ex = (
            record["raw_close"] - window["effective_cash_per_share"]
        ) / window["effective_share_multiplier"]
        event_factor = record["raw_close"] / theoretical_ex

        raw = pd.DataFrame(
            {
                "open": [item["raw_close"] for item in bars],
                "high": [item["raw_close"] for item in bars],
                "low": [item["raw_close"] for item in bars],
                "close": [item["raw_close"] for item in bars],
                "volume": [item["volume"] for item in bars],
                "amount": [item["amount_cny"] / 1000 for item in bars],
            },
            index=pd.DatetimeIndex(
                [item["trade_date"] for item in bars], name="trade_date"
            ),
        )
        context = AdjustmentContext(
            symbol=window["symbol"],
            source="akshare-cninfo-frozen",
            version=fixture["fetched_at"],
            as_of=bars[-1]["trade_date"],
            points=tuple(
                AdjustmentFactorPoint(
                    trade_date=item["trade_date"],
                    price_factor=(
                        event_factor if item["trade_date"] >= window["ex_date"] else 1.0
                    ),
                    share_factor=(
                        window["effective_share_multiplier"]
                        if item["trade_date"] >= window["ex_date"]
                        else 1.0
                    ),
                    known_at=f"{item['trade_date']}T15:01:00+08:00",
                )
                for item in bars
            ),
        )
        qfq = adjust_stock_frame(raw, adjustment="qfq", context=context).frame
        hfq = adjust_stock_frame(raw, adjustment="hfq", context=context).frame

        assert qfq.loc[window["record_date"], "close"] == pytest.approx(theoretical_ex)
        assert qfq.loc[window["ex_date"], "close"] == pytest.approx(ex_bar["raw_close"])
        assert hfq.loc[window["record_date"], "close"] == pytest.approx(record["raw_close"])
        assert hfq.loc[window["ex_date"], "close"] == pytest.approx(
            ex_bar["raw_close"] * event_factor
        )
        pd.testing.assert_series_equal(qfq["amount"], raw["amount"])

        if window["symbol"] == "301336.SZ":
            assert theoretical_ex == pytest.approx(record["provider_qfq_close"], abs=0.01)
