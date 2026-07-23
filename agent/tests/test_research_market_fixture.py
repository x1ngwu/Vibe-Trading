"""QE1 acceptance tests for the frozen, offline small-market corpus."""

from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.research.market_fixture import (
    MarketFixtureError,
    RawBar,
    load_market_fixture,
    normalize_raw_bars,
    point_in_time_value,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "research" / "qe1_market_v1.json"
_EXPECTED_FIXTURE_SHA256 = "fe7a7849c68a6626213dca437e8b9dede49ee251c0ca1afb05301abce7e5913f"
_EXPECTED_SNAPSHOT_SHA256 = "94beb19bb7cd7d65d867957be97573bbafc3508f6520086d79e14e8d1dfdd8cb"
_TOKYO = ZoneInfo("Asia/Tokyo")


def _load():
    return load_market_fixture(_FIXTURE)


def test_qe1_market_fixture_is_content_bound_and_has_required_market_shape() -> None:
    fixture = _load()
    counts = {
        kind: sum(item.instrument_type == kind for item in fixture.instruments)
        for kind in ("stock", "etf", "index")
    }

    assert fixture.fixture_sha256 == _EXPECTED_FIXTURE_SHA256
    assert fixture.snapshot_sha256 == _EXPECTED_SNAPSHOT_SHA256
    assert counts == {"stock": 8, "etf": 1, "index": 1}
    assert {item.board for item in fixture.instruments} >= {
        "sh_main",
        "sz_main",
        "chinext",
        "star",
        "etf",
        "index",
    }
    assert fixture.snapshot_ref().snapshot_sha256 == _EXPECTED_SNAPSHOT_SHA256
    assert set(fixture.snapshot_ref().actual_sources.values()) == {"fixture"}


def test_qe1_fixture_load_is_strictly_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("fixture reader attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    assert _load().fixture_id == "qe1-cn-small-market-v1"


def test_qe1_fixture_covers_calendar_missing_and_listing_states() -> None:
    fixture = _load()
    open_days = {day.trade_date for day in fixture.calendar if day.is_open}
    classifications = {
        (item.symbol, item.trade_date.isoformat()): item.classification
        for item in fixture.expected.missing
    }

    assert len(open_days) == 7
    assert classifications == {
        ("600005.SH", "2025-01-06"): "suspension",
        ("600006.SH", "2025-01-08"): "true_missing",
        ("000003.SZ", "2025-01-02"): "not_listed",
        ("600004.SH", "2025-01-10"): "delisted",
    }
    suspended = [
        row for row in fixture.raw_bars if row.symbol == "600005.SH" and row.status == "suspended"
    ]
    assert [row.trade_date.isoformat() for row in suspended] == ["2025-01-06", "2025-01-07"]


def test_qe1_identical_duplicates_collapse_and_conflicts_fail_closed() -> None:
    fixture = _load()
    identical = tuple(row for row in fixture.raw_bars if row.symbol == "600002.SH")
    conflicting = tuple(row for row in fixture.raw_bars if row.symbol == "600006.SH")

    normalized = normalize_raw_bars(identical)
    assert len(normalized) == 4
    assert [row.source_row_id for row in normalized] == ["r004", "r005a", "r005c", "r005d"]

    with pytest.raises(
        MarketFixtureError,
        match=r"600006\.SH on 2025-01-07: fields=\['amount', 'close', 'high'\]",
    ):
        normalize_raw_bars(conflicting)


def test_qe1_corporate_action_factors_and_adjustment_semantics_are_hand_calculated() -> None:
    fixture = _load()
    actions = {item.kind: item for item in fixture.corporate_actions}

    assert actions["cash_dividend"].single_event_factor == pytest.approx((20.0 - 0.5) / 20.0)
    assert actions["share_split"].single_event_factor == pytest.approx(1.0 / 2.0)
    assert actions["rights_issue"].single_event_factor == pytest.approx(
        ((10.0 + 0.2 * 5.0) / 1.2) / 10.0
    )
    assert fixture.adjustment_rules.amount == "never_adjust"
    assert fixture.adjustment_rules.volume == "divide_by_share_multiplier_only"
    assert fixture.adjustment_rules.qfq_anchor == "last_close_equals_raw"
    assert fixture.adjustment_rules.hfq_anchor == "first_close_equals_raw"


def test_qe1_point_in_time_metadata_respects_effective_and_known_at_dates() -> None:
    fixture = _load()

    before_industry_disclosure = datetime(2025, 1, 6, 23, 0, tzinfo=_TOKYO)
    after_industry_disclosure = datetime(2025, 1, 7, 9, 0, tzinfo=_TOKYO)
    before_financial_disclosure = datetime(2025, 1, 9, 17, 0, tzinfo=_TOKYO)
    after_financial_disclosure = datetime(2025, 1, 9, 19, 0, tzinfo=_TOKYO)

    assert point_in_time_value(
        fixture, symbol="600001.SH", field="industry", as_of=before_industry_disclosure
    ) == "consumer"
    assert point_in_time_value(
        fixture, symbol="600001.SH", field="industry", as_of=after_industry_disclosure
    ) == "consumer-services"
    assert point_in_time_value(
        fixture, symbol="600002.SH", field="financial_metric", as_of=before_financial_disclosure
    ) == 1.25
    assert point_in_time_value(
        fixture, symbol="600002.SH", field="financial_metric", as_of=after_financial_disclosure
    ) == 1.5


def test_qe1_fixture_freezes_rules_capacity_and_similarity_edge_samples() -> None:
    fixture = _load()
    rules = {item.rule for item in fixture.expected.execution_limits}
    relations = {item.relation for item in fixture.expected.similarity_samples}
    partial = next(
        item for item in fixture.expected.execution_limits if item.outcome == "partial_capacity"
    )

    assert rules == {"main_10pct", "st_5pct", "chinext_20pct", "star_20pct"}
    assert partial.maximum_fill_shares == 100
    assert relations == {"near", "reverse", "missing", "outlier"}


def test_qe1_future_market_mutation_does_not_change_historical_as_of_snapshot() -> None:
    fixture = _load()
    future_row = RawBar(
        source_row_id="future-r001",
        symbol="600001.SH",
        trade_date="2025-01-13",
        open=11.0,
        high=11.2,
        low=10.9,
        close=11.1,
        volume=123000,
        amount=1365300.0,
        status="traded",
        available_at="2025-01-13T15:01:00+08:00",
    )
    mutated = fixture.model_copy(update={"raw_bars": fixture.raw_bars + (future_row,)})

    assert mutated.fixture_sha256 != fixture.fixture_sha256
    assert mutated.snapshot_sha256 == fixture.snapshot_sha256
