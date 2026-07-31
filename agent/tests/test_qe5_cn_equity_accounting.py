"""QE5-1 deterministic A-share execution and exact-fen accounting tests."""

from __future__ import annotations

import json
import socket
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.quant_engine.cn_equity_accounting import (
    CnEquityAccount,
    CnEquityAccountingError,
    CnEquityFeeSchedule,
    CnEquityLedger,
    CnEquityOrder,
    calculate_cn_equity_fees,
)
from src.research.golden_ledger import load_golden_ledger
from src.research.market_fixture import load_market_fixture

_FIXTURES = Path(__file__).parent / "fixtures" / "research"
_MARKET = _FIXTURES / "qe1_market_v1.json"
_GOLDEN = _FIXTURES / "qe1_golden_ledger_v1.json"


def _rules() -> CnEquityFeeSchedule:
    return CnEquityFeeSchedule(
        commission_tenths_bps=30,
        minimum_commission_fen=500,
        sell_tax_tenths_bps=50,
        transfer_fee_tenths_bps=1,
        rule_version="cn-equity-2025-01-01",
    )


def _account(*, cash_fen: int = 10_000_000) -> CnEquityAccount:
    market = load_market_fixture(_MARKET)
    return CnEquityAccount(
        ledger_id="qe5-cn-account-v1",
        data_snapshot_sha256=market.snapshot_sha256,
        opening_date=date(2025, 1, 3),
        initial_cash_fen=cash_fen,
        rules=_rules(),
    )


def _order(
    order_id: str,
    trade_date: date,
    symbol: str,
    side: str,
    shares: int,
    price_fen: int,
    **kwargs,
) -> CnEquityOrder:
    return CnEquityOrder(
        order_id=order_id,
        trade_date=trade_date,
        symbol=symbol,
        side=side,
        requested_shares=shares,
        price_fen=price_fen,
        **kwargs,
    )


def test_qe5_oracle_replays_qe1_hand_calculated_ledger_exactly() -> None:
    account = _account()
    account.submit_order(
        _order(
            "buy-sh-main",
            date(2025, 1, 3),
            "600002.SH",
            "buy",
            1000,
            2000,
        )
    )
    account.submit_order(
        _order(
            "reject-same-day",
            date(2025, 1, 3),
            "600002.SH",
            "sell",
            1000,
            2000,
        )
    )
    account.submit_order(
        _order(
            "buy-sz-main",
            date(2025, 1, 3),
            "000003.SZ",
            "buy",
            1000,
            880,
        )
    )
    account.apply_share_split(
        trade_date=date(2025, 1, 6),
        symbol="000003.SZ",
        multiplier_numerator=2,
        multiplier_denominator=1,
        mark_prices_fen={"600002.SH": 1970, "000003.SZ": 450},
    )
    account.accrue_dividend(
        trade_date=date(2025, 1, 6),
        symbol="600002.SH",
        entitled_shares=1000,
        cash_per_share_fen=50,
        mark_prices_fen={"600002.SH": 1970, "000003.SZ": 450},
    )
    account.pay_dividend(
        trade_date=date(2025, 1, 8),
        symbol="600002.SH",
        mark_prices_fen={"600002.SH": 1990, "000003.SZ": 450},
    )
    account.submit_order(
        _order(
            "sell-sh-main",
            date(2025, 1, 9),
            "600002.SH",
            "sell",
            1000,
            2000,
        )
    )

    actual = account.ledger()
    expected = load_golden_ledger(_GOLDEN)

    assert len(actual.entries) == len(expected.steps)
    for actual_entry, expected_step in zip(actual.entries, expected.steps):
        assert actual_entry.sequence == expected_step.sequence
        assert actual_entry.trade_date == expected_step.trade_date
        assert actual_entry.position_delta == expected_step.position_delta
        assert actual_entry.cash_delta_fen == expected_step.cash_delta_fen
        assert (
            actual_entry.dividend_receivable_delta_fen
            == expected_step.dividend_receivable_delta_fen
        )
        assert actual_entry.fees.total_fen == expected_step.fee_fen
        assert actual_entry.positions == expected_step.positions
        assert actual_entry.cash_fen == expected_step.cash_fen
        assert (
            actual_entry.dividend_receivable_fen
            == expected_step.dividend_receivable_fen
        )
        assert actual_entry.mark_prices_fen == expected_step.mark_prices_fen
        assert actual_entry.market_value_fen == expected_step.market_value_fen
        assert actual_entry.equity_fen == expected_step.equity_fen

    assert actual.entries[2].reason == "T1_LOCKED"
    assert actual.entries[-1].fees.model_dump() == {
        "commission_fen": 600,
        "sell_tax_fen": 1000,
        "transfer_fee_fen": 20,
        "total_fen": 1620,
    }
    assert actual.entries[-1].equity_fen == 10_067_251


def test_fee_schedule_uses_per_charge_half_up_and_minimum_commission() -> None:
    small_buy = calculate_cn_equity_fees(
        shares=1000,
        price_fen=880,
        side="buy",
        rules=_rules(),
    )
    assert small_buy.model_dump() == {
        "commission_fen": 500,
        "sell_tax_fen": 0,
        "transfer_fee_fen": 9,
        "total_fen": 509,
    }

    sell = calculate_cn_equity_fees(
        shares=1000,
        price_fen=2000,
        side="sell",
        rules=_rules(),
    )
    assert sell.total_fen == 600 + 1000 + 20


def test_cash_dividend_supports_exact_sub_fen_per_share_rate() -> None:
    account = _account()
    account.submit_order(
        _order(
            "buy-rational-dividend",
            date(2025, 1, 3),
            "600002.SH",
            "buy",
            100,
            2_000,
        )
    )
    accrued = account.accrue_dividend(
        trade_date=date(2025, 1, 6),
        symbol="600002.SH",
        entitled_shares=100,
        cash_per_share_numerator_fen=155,
        cash_per_share_denominator=2,
        mark_prices_fen={"600002.SH": 1_970},
    )
    assert accrued.dividend_receivable_delta_fen == 7_750

    before = account.ledger()
    with pytest.raises(
        CnEquityAccountingError,
        match="not representable in integer fen",
    ):
        account.accrue_dividend(
            trade_date=date(2025, 1, 7),
            symbol="600002.SH",
            entitled_shares=100,
            cash_per_share_numerator_fen=1,
            cash_per_share_denominator=3,
            mark_prices_fen={"600002.SH": 1_980},
        )
    assert account.ledger() == before

    rounded = account.accrue_dividend(
        trade_date=date(2025, 1, 7),
        symbol="600002.SH",
        entitled_shares=100,
        cash_per_share_numerator_fen=2_700_039,
        cash_per_share_denominator=5_000,
        cash_rounding="half_up_total_fen",
        mark_prices_fen={"600002.SH": 1_980},
    )
    assert rounded.dividend_receivable_delta_fen == 54_001


def test_t_plus_one_rejection_has_no_effect_and_next_day_sell_is_allowed() -> None:
    account = _account()
    buy = account.submit_order(
        _order("buy", date(2025, 1, 3), "600002.SH", "buy", 100, 2000)
    )
    rejected = account.submit_order(
        _order("same-day", date(2025, 1, 3), "600002.SH", "sell", 100, 2000)
    )
    sold = account.submit_order(
        _order("next-day", date(2025, 1, 6), "600002.SH", "sell", 100, 2000)
    )

    assert buy.outcome == "filled"
    assert rejected.outcome == "rejected"
    assert rejected.reason == "T1_LOCKED"
    assert rejected.cash_delta_fen == 0
    assert rejected.position_delta == {}
    assert sold.outcome == "filled"
    assert sold.positions == {}


def test_buy_lot_rounding_capacity_and_odd_lot_sell_are_explicit() -> None:
    account = _account()
    partial = account.submit_order(
        _order(
            "capacity",
            date(2025, 1, 3),
            "600002.SH",
            "buy",
            250,
            2000,
            maximum_fill_shares=150,
        )
    )
    odd_lot_sale = account.submit_order(
        _order(
            "odd-lot-sell",
            date(2025, 1, 6),
            "600002.SH",
            "sell",
            50,
            2000,
        )
    )

    assert partial.outcome == "partially_filled"
    assert partial.reason == "CAPACITY_LIMITED"
    assert partial.filled_shares == 100
    assert odd_lot_sale.outcome == "filled"
    assert odd_lot_sale.filled_shares == 50
    assert odd_lot_sale.positions == {"600002.SH": 50}


@pytest.mark.parametrize(
    ("side", "market_state", "reason"),
    [
        ("buy", "suspended", "SUSPENDED"),
        ("buy", "locked_limit_up", "LIMIT_UP_LOCKED"),
        ("sell", "locked_limit_down", "LIMIT_DOWN_LOCKED"),
        ("buy", "delisted", "DELISTED"),
    ],
)
def test_market_rejections_are_typed_and_have_no_accounting_effect(
    side: str,
    market_state: str,
    reason: str,
) -> None:
    account = _account()
    if side == "sell":
        account.submit_order(
            _order("seed", date(2025, 1, 3), "600005.SH", "buy", 100, 500)
        )
        trade_date = date(2025, 1, 6)
    else:
        trade_date = date(2025, 1, 3)
    before = account.ledger().entries[-1]
    rejected = account.submit_order(
        _order(
            f"reject-{reason}",
            trade_date,
            "600005.SH",
            side,
            100,
            500,
            market_state=market_state,
        )
    )

    assert rejected.outcome == "rejected"
    assert rejected.reason == reason
    assert rejected.cash_fen == before.cash_fen
    assert rejected.positions == before.positions
    assert rejected.fees.total_fen == 0


def test_insufficient_cash_and_position_fail_closed() -> None:
    account = _account(cash_fen=1000)
    no_cash = account.submit_order(
        _order("too-large", date(2025, 1, 3), "600002.SH", "buy", 100, 2000)
    )
    no_position = account.submit_order(
        _order("no-position", date(2025, 1, 3), "600002.SH", "sell", 100, 2000)
    )

    assert no_cash.reason == "INSUFFICIENT_CASH"
    assert no_position.reason == "NO_POSITION"
    assert account.cash_fen == 1000
    assert account.positions == {}


def test_fractional_share_action_and_incomplete_marks_fail_closed() -> None:
    account = _account()
    account.submit_order(
        _order("buy", date(2025, 1, 3), "000003.SZ", "buy", 100, 880)
    )

    with pytest.raises(
        CnEquityAccountingError,
        match="fractional share",
    ):
        account.apply_share_split(
            trade_date=date(2025, 1, 6),
            symbol="000003.SZ",
            multiplier_numerator=3,
            multiplier_denominator=8,
            mark_prices_fen={"000003.SZ": 450},
        )

    with pytest.raises(CnEquityAccountingError, match="marks must cover"):
        account.mark(trade_date=date(2025, 1, 6), mark_prices_fen={})


def test_canonical_ledger_is_deterministic_and_tamper_evident() -> None:
    first = _account()
    second = _account()
    for account in (first, second):
        account.submit_order(
            _order("buy", date(2025, 1, 3), "600002.SH", "buy", 100, 2000)
        )

    first_ledger = first.ledger()
    second_ledger = second.ledger()
    assert first_ledger == second_ledger
    assert first_ledger.content_sha256 == second_ledger.content_sha256

    tampered = json.loads(first_ledger.model_dump_json())
    tampered["entries"][-1]["cash_fen"] += 1
    tampered["entries"][-1]["equity_fen"] += 1
    with pytest.raises(ValidationError, match="cash invariant failed"):
        CnEquityLedger.model_validate(tampered)

    forged_cash_flow = json.loads(first_ledger.model_dump_json())
    forged_cash_flow["entries"][-1]["cash_delta_fen"] += 1
    forged_cash_flow["entries"][-1]["cash_fen"] += 1
    forged_cash_flow["entries"][-1]["equity_fen"] += 1
    with pytest.raises(ValidationError, match="trade cash flow"):
        CnEquityLedger.model_validate(forged_cash_flow)


def test_qe5_accounting_oracle_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("QE5 accounting oracle attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    account = _account()
    account.submit_order(
        _order("buy", date(2025, 1, 3), "600002.SH", "buy", 100, 2000)
    )
    assert account.ledger().entries[-1].outcome == "filled"
