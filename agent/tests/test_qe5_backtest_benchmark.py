"""QE5-6 PR-03 benchmark fixture and audit-shape regression."""

from __future__ import annotations

from datetime import date

from scripts.qe5_backtest_benchmark import build_benchmark_snapshot


def test_qe5_pr03_fixture_is_deterministic_target_shape_and_disclosed() -> None:
    first, symbols, dates = build_benchmark_snapshot(
        symbol_count=10,
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
    )
    second, second_symbols, second_dates = build_benchmark_snapshot(
        symbol_count=10,
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
    )

    assert first == second
    assert symbols == second_symbols
    assert dates == second_dates
    assert len(symbols) == 10
    assert len(first["bars"]) == len(symbols) * len(dates)
    assert first["corporate_actions"] == []
    assert first["price_semantics"] == {
        "execution_price_adjustment": "raw",
        "signal_price_adjustment": "qfq",
        "corporate_action_mode": "explicit",
    }
    assert all(item["known_at"].endswith("+08:00") for item in first["bars"])
    assert set(first["bars"][0]["features"]) == set()
