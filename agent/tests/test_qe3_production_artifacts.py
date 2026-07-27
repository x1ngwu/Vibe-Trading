"""QE3 seventh-slice production source and 300-name shard tests."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest.loaders.data_envelope import (
    DataEnvelope,
    DataEnvelopeManifest,
    DataFetchRequest,
    OfflineDataSnapshot,
    SourceAttempt,
    SymbolOutcome,
)
from src.research import (
    ProductionArtifactError,
    build_csi300_csindex_source_batch,
    build_factor_snapshot_from_price_volume,
    build_price_volume_snapshot_from_bundle,
    build_qe3_data_snapshot_bundle,
    build_tushare_business_feature_snapshot,
    materialize_csi300_csindex_universe,
    validate_qe3_production_artifacts,
)

AS_OF = date(2026, 7, 27)
MEMBERSHIP_DATE = date(2026, 7, 24)
WEIGHT_DATE = date(2026, 6, 30)
CAPTURED_AT = datetime(2026, 7, 27, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
DATES = pd.to_datetime(["2026-07-23", "2026-07-24", "2026-07-27"])
FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _symbol(index: int) -> str:
    if index < 150:
        return f"{index + 1:06d}.SZ"
    return f"{600000 + index:06d}.SH"


def _source_rows():
    constituent_rows = []
    weight_rows = []
    stock_rows = []
    standard_weight = Decimal("100") / Decimal("300")
    for index in range(300):
        symbol = _symbol(index)
        code, suffix = symbol.split(".")
        exchange_name = "深圳证券交易所" if suffix == "SZ" else "上海证券交易所"
        exchange = "SZSE" if suffix == "SZ" else "SSE"
        weight = (
            Decimal("100") - standard_weight * 299
            if index == 299
            else standard_weight
        )
        constituent_rows.append(
            {
                "日期": MEMBERSHIP_DATE,
                "指数代码": "000300",
                "成分券代码": code,
                "成分券名称": f"sample-{index:03d}",
                "交易所": exchange_name,
            }
        )
        weight_rows.append(
            {
                "日期": WEIGHT_DATE,
                "指数代码": "000300",
                "成分券代码": code,
                "成分券名称": f"sample-{index:03d}",
                "交易所": exchange_name,
                "权重": weight,
            }
        )
        stock_rows.append(
            {
                "ts_code": symbol,
                "exchange": exchange,
                "list_status": "L",
                "list_date": "20100101",
                "delist_date": "",
                "industry": "sample",
            }
        )
    return constituent_rows, weight_rows, stock_rows


def _source_batch():
    return build_csi300_csindex_source_batch(
        *_source_rows(),
        batch_id="csi300-20260727-production",
        source_version="akshare-1.17.87",
        as_of=AS_OF,
        captured_at=CAPTURED_AT,
    )


def _frame(index: int) -> pd.DataFrame:
    base = 10.0 + index / 100.0
    return pd.DataFrame(
        {
            "open": [base, base + 0.1, base + 0.2],
            "high": [base + 0.2, base + 0.3, base + 0.4],
            "low": [base - 0.1, base, base + 0.1],
            "close": [base + 0.1, base + 0.2, base + 0.3],
            "volume": [1000.0 + index, 1100.0 + index, 1200.0 + index],
            "amount": [10000.0 + index, 11000.0 + index, 12000.0 + index],
        },
        index=pd.DatetimeIndex(DATES, name="trade_date"),
    )


def _envelope(symbols: tuple[str, ...], frames: dict[str, pd.DataFrame]) -> DataEnvelope:
    request = DataFetchRequest(
        symbols=symbols,
        instrument_types={symbol: "stock" for symbol in symbols},
        start_date=DATES[0].date(),
        end_date=AS_OF,
        adjustment="qfq",
        fields=FIELDS,
        requested_sources=("akshare",),
    )
    actual_sources = {symbol: "akshare" for symbol in symbols}
    units = {
        symbol: {
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "lot_100_shares",
            "amount": "CNY_1000",
        }
        for symbol in symbols
    }
    outcomes = tuple(
        SymbolOutcome(
            symbol=symbol,
            status="ok",
            attempted_sources=("akshare",),
            actual_source="akshare",
            row_count=3,
        )
        for symbol in symbols
    )
    attempts = tuple(
        SourceAttempt(
            symbol=symbol,
            source="akshare",
            status="selected",
            detail="selected 3 normalized rows",
        )
        for symbol in symbols
    )
    bars = {
        symbol: tuple(
            {
                "trade_date": trade_date.date().isoformat(),
                **{field: float(row[field]) for field in FIELDS},
            }
            for trade_date, row in frames[symbol].iterrows()
        )
        for symbol in symbols
    }
    artifact = OfflineDataSnapshot(
        request=request,
        source_versions={"akshare": "akshare-qfq-1.17.87"},
        actual_sources=actual_sources,
        units=units,
        outcomes=outcomes,
        anomalies=(),
        source_attempts=attempts,
        bars=bars,
    )
    manifest = DataEnvelopeManifest(
        request_sha256=request.request_sha256,
        snapshot_sha256=artifact.snapshot_sha256,
        symbols=symbols,
        instrument_types=request.instrument_types,
        start_date=request.start_date,
        end_date=request.end_date,
        interval="1D",
        adjustment="qfq",
        fields=FIELDS,
        requested_sources=("akshare",),
        source_versions=artifact.source_versions,
        actual_sources=actual_sources,
        units=units,
        outcomes=outcomes,
        anomalies=(),
        source_attempts=attempts,
    )
    return DataEnvelope(
        request=request,
        manifest=manifest,
        frames={symbol: frames[symbol] for symbol in symbols},
    )


def _production_inputs():
    source = _source_batch()
    symbols = source.symbols
    frames = {symbol: _frame(index) for index, symbol in enumerate(symbols)}
    envelopes = tuple(
        _envelope(symbols[offset : offset + 100], frames)
        for offset in range(0, 300, 100)
    )
    bundle = build_qe3_data_snapshot_bundle(envelopes)
    price_volume = build_price_volume_snapshot_from_bundle(
        bundle,
        envelopes,
        window_days=3,
        snapshot_id="qe3-production-price-volume-20260727",
    )
    factors = build_factor_snapshot_from_price_volume(
        price_volume,
        snapshot_id="qe3-production-factors-20260727",
        source_version="akshare-qfq-1.17.87",
        known_at=CAPTURED_AT,
        factor_window_days=3,
    )
    _, _, stock_rows = _source_rows()
    business = build_tushare_business_feature_snapshot(
        stock_rows,
        [
            {
                "ts_code": symbol,
                "trade_date": "20260727",
                "total_mv": 1_000_000.0 + index,
            }
            for index, symbol in enumerate(symbols)
        ],
        [
            {
                "ts_code": symbol,
                "trade_date": trade_date.strftime("%Y%m%d"),
                "amount": float(frames[symbol].loc[trade_date, "amount"]),
            }
            for symbol in symbols
            for trade_date in DATES
        ],
        symbols=symbols,
        snapshot_id="qe3-production-business-20260727",
        source_version="tushare-1.4.24",
        as_of=AS_OF,
        market_trade_date=AS_OF,
        captured_at=CAPTURED_AT,
        liquidity_window_days=3,
    )
    return source, bundle, business, factors, price_volume


def test_official_csindex_batch_is_complete_content_bound_and_materializable() -> None:
    first = _source_batch()
    constituent_rows, weight_rows, stock_rows = _source_rows()
    second = build_csi300_csindex_source_batch(
        tuple(reversed(constituent_rows)),
        tuple(reversed(weight_rows)),
        tuple(reversed(stock_rows)),
        batch_id=first.batch_id,
        source_version=first.source_version,
        as_of=AS_OF,
        captured_at=CAPTURED_AT,
    )

    assert first == second
    assert len(first.symbols) == 300
    assert first.source_content_sha256 == second.source_content_sha256
    universe = materialize_csi300_csindex_universe(first)
    assert universe.snapshot(AS_OF).active_symbols == first.symbols


def test_official_csindex_batch_rejects_partial_or_mismatched_sources() -> None:
    constituent_rows, weight_rows, stock_rows = _source_rows()
    with pytest.raises(ProductionArtifactError, match="weight missing"):
        build_csi300_csindex_source_batch(
            constituent_rows,
            weight_rows[:-1],
            stock_rows,
            batch_id="partial",
            source_version="akshare-1.17.87",
            as_of=AS_OF,
            captured_at=CAPTURED_AT,
        )

    changed = [dict(item) for item in stock_rows]
    changed[0]["exchange"] = "SSE"
    with pytest.raises(ProductionArtifactError, match="exchange mismatch"):
        build_csi300_csindex_source_batch(
            constituent_rows,
            weight_rows,
            changed,
            batch_id="mismatch",
            source_version="akshare-1.17.87",
            as_of=AS_OF,
            captured_at=CAPTURED_AT,
        )


def test_three_qe2_shards_bind_full_production_feature_artifacts() -> None:
    source, bundle, business, factors, price_volume = _production_inputs()
    universe = materialize_csi300_csindex_universe(source)

    assert [len(item.symbols) for item in bundle.shards] == [100, 100, 100]
    assert len(price_volume.records) == len(factors.records) == 300
    assert price_volume.data_snapshot_sha256 == bundle.bundle_sha256
    assert {item.values[0].source for item in factors.records} == {"akshare"}
    validate_qe3_production_artifacts(
        source,
        universe,
        bundle,
        business,
        factors,
        price_volume,
    )


def test_bundle_rejects_missing_or_reordered_shards() -> None:
    source = _source_batch()
    symbols = source.symbols
    frames = {symbol: _frame(index) for index, symbol in enumerate(symbols)}
    envelopes = tuple(
        _envelope(symbols[offset : offset + 100], frames)
        for offset in range(0, 300, 100)
    )
    with pytest.raises(ProductionArtifactError, match="exactly three shards"):
        build_qe3_data_snapshot_bundle(envelopes[:2])

    changed_request = envelopes[1].request.model_copy(
        update={"symbols": tuple(reversed(envelopes[1].request.symbols))}
    )
    changed = SimpleNamespace(
        request=changed_request,
        manifest=envelopes[1].manifest,
        require_complete=lambda: None,
    )
    with pytest.raises(ProductionArtifactError, match="symbols must be sorted"):
        build_qe3_data_snapshot_bundle((envelopes[0], changed, envelopes[2]))
