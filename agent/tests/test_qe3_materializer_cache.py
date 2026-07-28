"""QE3 production materializer source-cache regressions."""

from __future__ import annotations
import stat
from datetime import date


import pandas as pd
import pytest

from scripts.qe3_materialize_production import (
    _build_envelopes,
    _cached_source_frame,
    _normalize_baostock_stock_metadata,
    _normalized_baostock_qfq,
    _split_baostock_suspension_rows,
)


def test_successful_upstream_table_is_persisted_and_reused(tmp_path) -> None:
    cache_path = tmp_path / "source-cache" / "upstream.json"
    calls = 0

    def fetch() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260724",
                    "total_mv": 123.45,
                }
            ]
        )

    first = _cached_source_frame(cache_path, fetch)
    second = _cached_source_frame(
        cache_path,
        lambda: (_ for _ in ()).throw(AssertionError("unexpected upstream call")),
    )

    assert calls == 1
    pd.testing.assert_frame_equal(second, first)
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_baostock_metadata_is_strictly_normalized_without_future_industry() -> None:
    basic = pd.DataFrame(
        [
            {
                "code": "sh.600519",
                "code_name": "贵州茅台",
                "ipoDate": "2001-08-27",
                "outDate": "",
                "type": "1",
                "status": "1",
            },
            {
                "code": "sz.000001",
                "code_name": "平安银行",
                "ipoDate": "1991-04-03",
                "outDate": "",
                "type": "1",
                "status": "1",
            },
        ]
    )
    industry = pd.DataFrame(
        [
            {"updateDate": "2026-07-27", "code": "sh.600519", "industry": "食品饮料"},
            {"updateDate": "2026-07-27", "code": "sz.000001", "industry": "银行"},
        ]
    )

    result = _normalize_baostock_stock_metadata(
        basic,
        industry,
        as_of=date(2026, 7, 28),
    )

    assert result["ts_code"].tolist() == ["000001.SZ", "600519.SH"]
    assert result["exchange"].tolist() == ["SZSE", "SSE"]
    assert result["list_status"].tolist() == ["L", "L"]
    assert result["list_date"].tolist() == ["19910403", "20010827"]
    assert result["industry"].tolist() == ["银行", "食品饮料"]

    future = industry.copy()
    future.loc[0, "updateDate"] = "2026-07-29"
    with pytest.raises(RuntimeError, match="future updateDate"):
        _normalize_baostock_stock_metadata(basic, future, as_of=date(2026, 7, 28))


def test_baostock_qfq_normalizes_documented_volume_and_amount_units() -> None:
    class Result:
        error_code = "0"
        error_msg = "success"
        fields = ["date", "open", "high", "low", "close", "volume", "amount", "tradestatus"]

        def __init__(self) -> None:
            self.rows = [
                ["2026-07-23", "10", "11", "9", "10.5", "10000", "200000", "1"],
                ["2026-07-24", "11", "12", "10", "11.5", "12000", "240000", "1"],
                ["2026-07-27", "12", "13", "11", "12.5", "14000", "280000", "1"],
            ]
            self.index = 0

        def next(self) -> bool:
            return self.index < len(self.rows)

        def get_row_data(self) -> list[str]:
            row = self.rows[self.index]
            self.index += 1
            return row

    class BaoStock:
        def query_history_k_data_plus(self, code, fields, **kwargs):
            assert code == "sh.600519"
            assert fields == "date,open,high,low,close,volume,amount,tradestatus"
            assert kwargs == {
                "start_date": "2026-07-23",
                "end_date": "2026-07-27",
                "frequency": "d",
                "adjustflag": "2",
            }
            return Result()

    frame = _normalized_baostock_qfq(
        BaoStock(),
        "600519.SH",
        date(2026, 7, 23),
        date(2026, 7, 27),
    )

    assert frame.index.name == "trade_date"
    assert frame["volume"].tolist() == [100.0, 120.0, 140.0]
    assert frame["amount"].tolist() == [200.0, 240.0, 280.0]
    assert frame["tradestatus"].tolist() == ["1", "1", "1"]


def test_baostock_legacy_null_turnover_becomes_explicit_suspension() -> None:
    dates = pd.to_datetime(
        [
            "2026-07-21",
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
            "2026-07-27",
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [9.8, 10.0, 10.5, 10.5, 10.6],
            "high": [10.0, 10.2, 10.5, 10.5, 10.8],
            "low": [9.7, 9.9, 10.5, 10.5, 10.4],
            "close": [9.9, 10.1, 10.5, 10.5, 10.7],
            "volume": [90.0, 100.0, float("nan"), float("nan"), 140.0],
            "amount": [180.0, 200.0, float("nan"), float("nan"), 280.0],
        },
        index=pd.DatetimeIndex(dates, name="trade_date"),
    )

    active, suspensions = _split_baostock_suspension_rows(
        frame,
        symbol="600519.SH",
    )

    assert active.index.strftime("%Y-%m-%d").tolist() == [
        "2026-07-21",
        "2026-07-22",
        "2026-07-27",
    ]
    assert suspensions == (date(2026, 7, 23), date(2026, 7, 24))
    assert not active.isna().any().any()

    conflicting = frame.copy()
    conflicting.loc[dates[2], "amount"] = 1.0
    with pytest.raises(RuntimeError, match="nullability differs"):
        _split_baostock_suspension_rows(conflicting, symbol="600519.SH")


def test_baostock_qfq_frames_bind_to_the_requested_envelope_source() -> None:
    symbols = ("000001.SZ",)
    dates = pd.to_datetime(["2026-07-23", "2026-07-24", "2026-07-27"])
    frames = {
        symbols[0]: pd.DataFrame(
            {
                "open": [10.0, 10.1, 10.2],
                "high": [10.2, 10.3, 10.4],
                "low": [9.9, 10.0, 10.1],
                "close": [10.1, 10.2, 10.3],
                "volume": [100.0, 120.0, 140.0],
                "amount": [200.0, 240.0, 280.0],
            },
            index=pd.DatetimeIndex(dates, name="trade_date"),
        )
    }

    envelopes = _build_envelopes(
        frames,
        start_date=date(2026, 7, 23),
        end_date=date(2026, 7, 27),
        source="baostock",
        source_version="baostock-qfq-00.9.30",
    )

    assert len(envelopes) == 1
    assert envelopes[0].manifest.actual_sources == {"000001.SZ": "baostock"}
    assert envelopes[0].manifest.outcomes[0].status == "ok"
