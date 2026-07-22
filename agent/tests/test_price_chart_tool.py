from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import pytest

from src.session.service import load_visualization_specs
from src.tools import price_chart_tool


class _TencentLoader:
    name = "tencent"


class _EastmoneyLoader:
    name = "eastmoney"


class _YahooLoader:
    name = "yahoo"


def test_system_prompt_routes_chart_requests_to_price_chart_tool() -> None:
    from src.agent.context import _SYSTEM_PROMPT

    assert "**Chat price chart**" in _SYSTEM_PROMPT
    assert "Call `show_price_chart` directly" in _SYSTEM_PROMPT
    chart_section = _SYSTEM_PROMPT.split("**Chat price chart**", 1)[1].split("**Backtest**", 1)[0]
    advertised = set(re.findall(r"`(1m|5m|15m|30m|1H|4H|1D)`", chart_section))
    schema_intervals = set(price_chart_tool.PriceChartTool.parameters["properties"]["interval"]["enum"])
    assert advertised == schema_intervals
    assert "latest five years" in _SYSTEM_PROMPT


def test_price_chart_declares_current_run_scope() -> None:
    assert price_chart_tool.PriceChartTool.requires_current_run_dir is True


@pytest.mark.parametrize("symbol", ["^GSPC", "^IXIC", "^DJI"])
def test_price_chart_preserves_verified_us_index_symbols(symbol) -> None:
    assert price_chart_tool._normalize_symbol(symbol) == symbol
    assert price_chart_tool._market_for(symbol) == "US"


def test_price_chart_rejects_unsupported_yahoo_index() -> None:
    with pytest.raises(ValueError, match="unsupported index symbol.*\\^RUT"):
        price_chart_tool.PriceChartTool().execute(
            codes=["^RUT"], run_dir="unused"
        )


def test_price_chart_rejects_symlinked_artifact_escape(monkeypatch, tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    artifacts = tmp_path / "artifacts"
    try:
        artifacts.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)

    with pytest.raises(ValueError, match="escapes the workspace root"):
        price_chart_tool.PriceChartTool().execute(
            codes=["AAPL.US"],
            run_dir="ignored-in-test",
        )


def test_price_chart_tool_persists_compact_manifest(monkeypatch, tmp_path) -> None:
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return {
            "600519.SH": [
                {"trade_date": "2026-07-18", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
                {"trade_date": "2026-07-21", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 120},
            ]
        }

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _TencentLoader)
    monkeypatch.setattr(price_chart_tool, "fetch_market_data", fake_fetch)

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["600519"],
        start_date="2026-07-01",
        end_date="2026-07-21",
        run_dir="ignored-in-test",
    ))

    assert result["status"] == "ok"
    assert "bars" not in result
    assert calls["codes"] == ["600519.SH"]
    assert calls["source"] == "tencent"
    assert calls["max_rows"] == 0

    spec = result["visualizations"][0]
    assert spec["source"] == "tencent"
    assert spec["adjustment"] == "qfq"
    assert spec["timezone"] == "Asia/Shanghai"
    assert spec["bar_count"] == 2

    manifest = json.loads((tmp_path / "artifacts" / "visualizations.json").read_text())
    assert manifest == [spec]
    payload = json.loads(
        (tmp_path / "artifacts" / "visualizations" / f"{spec['visualization_id']}.json").read_text()
    )
    assert payload["bars"][0]["time"] == "2026-07-18"
    assert payload["bars"][1]["close"] == 12.0


@pytest.mark.parametrize(
    ("symbol", "source", "interval", "max_span_days"),
    [
        ("BTC-USDT", "okx", "1m", 4),
        ("600519.SH", "eastmoney", "1m", 40),
        ("AAPL.US", "yahoo", "1m", 7),
        ("AAPL.US", "yahoo", "1H", 730),
    ],
)
def test_intraday_fetch_window_is_bounded_before_loader(
    symbol: str,
    source: str,
    interval: str,
    max_span_days: int,
) -> None:
    requested_start = "2020-01-01"
    end_date = "2026-07-21"

    fetch_start = price_chart_tool._effective_fetch_start(
        symbol,
        source,
        interval,
        requested_start,
        end_date,
    )

    assert fetch_start > requested_start
    assert (date.fromisoformat(end_date) - date.fromisoformat(fetch_start)).days <= max_span_days
    assert price_chart_tool._effective_fetch_start(
        symbol,
        source,
        interval,
        "2026-07-20",
        end_date,
    ) == "2026-07-20"


def test_normalize_bars_drops_invalid_rows_deduplicates_and_sorts() -> None:
    def make_bar(time: str, **updates):
        row = {
            "trade_date": time,
            "open": 10,
            "high": 12,
            "low": 9,
            "close": 11,
            "volume": 100,
        }
        row.update(updates)
        return row

    rows = [
        make_bar("2026-07-21 10:01:00"),
        make_bar("2026-07-21 10:00:00", close=10),
        make_bar("2026-07-21 10:00:00", close=10.5),
        make_bar("not-a-time"),
        make_bar("2026-07-21 10:02:00", open=0),
        make_bar("2026-07-21 10:03:00", high=10, close=11),
        make_bar("2026-07-21 10:04:00", volume=-1),
    ]

    bars, truncated, dropped_bar_count = price_chart_tool._normalize_bars(rows, "1m")

    assert truncated is False
    assert dropped_bar_count == 5
    assert [bar["time"] for bar in bars] == [
        "2026-07-21T10:00:00",
        "2026-07-21T10:01:00",
    ]
    assert bars[0]["close"] == 10.5


def test_price_chart_tool_routes_a_share_intraday_and_preserves_timestamp(monkeypatch, tmp_path) -> None:
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return {
            "600519.SH": [
                {
                    "trade_date": "2026-07-21 09:35:00",
                    "open": 10,
                    "high": 12,
                    "low": 9,
                    "close": 11,
                    "volume": 100,
                },
            ]
        }

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _EastmoneyLoader)
    monkeypatch.setattr(price_chart_tool, "fetch_market_data", fake_fetch)

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["600519"],
        interval="5m",
        start_date="2026-07-21",
        end_date="2026-07-21",
        run_dir="ignored-in-test",
    ))

    assert result["status"] == "ok"
    assert calls["source"] == "eastmoney"
    assert calls["interval"] == "5m"
    spec = result["visualizations"][0]
    assert spec["timeframe"] == "5m"
    assert spec["source"] == "eastmoney"
    assert spec["timezone"] == "Asia/Shanghai"
    payload = json.loads(
        (tmp_path / "artifacts" / "visualizations" / f"{spec['visualization_id']}.json").read_text()
    )
    assert payload["bars"][0]["time"] == "2026-07-21T09:35:00"


def test_price_chart_tool_reports_effective_fetch_range_for_long_intraday_request(
    monkeypatch,
    tmp_path,
) -> None:
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return {
            "AAPL.US": [
                {
                    "trade_date": "2026-07-21T14:30:00",
                    "open": 10,
                    "high": 12,
                    "low": 9,
                    "close": 11,
                    "volume": 100,
                },
            ]
        }

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _YahooLoader)
    monkeypatch.setattr(price_chart_tool, "fetch_market_data", fake_fetch)

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["AAPL.US"],
        interval="1m",
        source="yahoo",
        start_date="2020-01-01",
        end_date="2026-07-21",
        run_dir="ignored-in-test",
    ))

    assert calls["start_date"] > "2020-01-01"
    spec = result["visualizations"][0]
    assert spec["requested_start"] == "2020-01-01"
    assert spec["effective_fetch_start"] == calls["start_date"]
    assert spec["effective_fetch_end"] == "2026-07-21"
    assert spec["retention_policy"] == "latest_contiguous_up_to_5000_bars"
    assert spec["truncated"] is True


def test_price_chart_tool_keeps_latest_bars_contiguous_when_bounded(monkeypatch, tmp_path) -> None:
    first = datetime(2026, 7, 1, 0, 0)
    rows = [
        {
            "trade_date": (first + timedelta(minutes=index)).isoformat(),
            "open": 10,
            "high": 12,
            "low": 9,
            "close": 11,
            "volume": 100,
        }
        for index in range(5002)
    ]

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _YahooLoader)
    monkeypatch.setattr(
        price_chart_tool,
        "fetch_market_data",
        lambda **_kwargs: {"AAPL.US": rows},
    )

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["AAPL.US"],
        interval="1m",
        source="yahoo",
        start_date="2026-07-01",
        end_date="2026-07-05",
        run_dir="ignored-in-test",
    ))

    spec = result["visualizations"][0]
    assert spec["bar_count"] == 5000
    assert spec["truncated"] is True
    payload = json.loads(
        (tmp_path / "artifacts" / "visualizations" / f"{spec['visualization_id']}.json").read_text()
    )
    assert payload["bars"][0]["time"] == (first + timedelta(minutes=2)).isoformat()
    assert payload["bars"][-1]["time"] == (first + timedelta(minutes=5001)).isoformat()


def test_price_chart_tool_routes_long_a_share_daily_window_to_eastmoney(monkeypatch, tmp_path) -> None:
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return {
            "600519.SH": [
                {"trade_date": "2021-07-21", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
            ]
        }

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _EastmoneyLoader)
    monkeypatch.setattr(price_chart_tool, "fetch_market_data", fake_fetch)

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["600519"],
        start_date="2021-07-21",
        end_date="2026-07-21",
        run_dir="ignored-in-test",
    ))

    assert result["status"] == "ok"
    assert calls["source"] == "eastmoney"


def test_price_chart_tool_routes_beijing_exchange_daily_to_eastmoney(monkeypatch, tmp_path) -> None:
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return {
            "430139.BJ": [
                {"trade_date": "2026-07-21", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
            ]
        }

    monkeypatch.setattr(price_chart_tool, "safe_run_dir", lambda _value: tmp_path)
    monkeypatch.setattr(price_chart_tool, "get_loader", lambda _source: _EastmoneyLoader)
    monkeypatch.setattr(price_chart_tool, "fetch_market_data", fake_fetch)

    result = json.loads(price_chart_tool.PriceChartTool().execute(
        codes=["430139.BJ"],
        start_date="2026-07-01",
        end_date="2026-07-21",
        run_dir="ignored-in-test",
    ))

    assert result["status"] == "ok"
    assert calls["source"] == "eastmoney"


def test_price_chart_tool_rejects_ambiguous_month_interval() -> None:
    with pytest.raises(ValueError, match="use '1m'"):
        price_chart_tool.PriceChartTool().execute(codes=["AAPL.US"], interval="1M", run_dir="unused")


def test_price_chart_tool_rejects_more_than_five_symbols() -> None:
    with pytest.raises(ValueError, match="at most 5"):
        price_chart_tool.PriceChartTool().execute(
            codes=[f"SYM{i}.US" for i in range(6)],
            run_dir="unused",
        )


def test_visualization_manifest_loader_sanitizes_entries(tmp_path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "visualizations.json").write_text(json.dumps([
        {
            "schema_version": 1,
            "type": "candlestick_volume",
            "visualization_id": "kline_safe",
            "data_ref": "kline_safe",
            "symbol": "AAPL.US",
            "effective_fetch_start": "2026-07-15",
            "effective_fetch_end": "2026-07-21",
            "retention_policy": "latest_contiguous_up_to_5000_bars",
            "bar_count": 2,
            "truncated": True,
            "dropped_bar_count": 3,
            "unexpected": {"do_not_forward": True},
        },
        {
            "schema_version": 1,
            "type": "candlestick_volume",
            "visualization_id": "../escape",
            "data_ref": "../escape",
        },
    ]), encoding="utf-8")

    assert load_visualization_specs(tmp_path) == [{
        "schema_version": 1,
        "type": "candlestick_volume",
        "visualization_id": "kline_safe",
        "data_ref": "kline_safe",
        "symbol": "AAPL.US",
        "effective_fetch_start": "2026-07-15",
        "effective_fetch_end": "2026-07-21",
        "retention_policy": "latest_contiguous_up_to_5000_bars",
        "bar_count": 2,
        "truncated": True,
        "dropped_bar_count": 3,
    }]
