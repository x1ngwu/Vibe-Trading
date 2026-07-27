#!/usr/bin/env python3
"""Materialize the QE3 seventh-slice production artifacts.

Online access is isolated to this command. The resulting directory contains
only content-bound JSON artifacts that the research path can replay offline.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from backtest.loaders.base import cached_loader_fetch  # noqa: E402
from backtest.loaders.data_envelope import (  # noqa: E402
    DataFetchRequest,
    LoaderCapability,
    fetch_data_envelope,
    write_offline_snapshot,
)
from src.research import (  # noqa: E402
    Qe3ArtifactRef,
    Qe3ProductionArtifactManifest,
    build_csi300_csindex_source_batch,
    build_factor_snapshot_from_price_volume,
    build_price_volume_snapshot_from_bundle,
    build_qe3_data_snapshot_bundle,
    build_tushare_business_feature_snapshot,
    canonical_json,
    canonical_sha256,
    materialize_csi300_csindex_universe,
    validate_qe3_production_artifacts,
)
from src.config.accessor import reset_env_config  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _normalized_akshare_qfq(
    ak: Any,
    symbol: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    frame = ak.stock_zh_a_hist(
        symbol=symbol.split(".")[0],
        period="daily",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        adjust="qfq",
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"AKShare returned no qfq rows for {symbol}")
    frame = frame.rename(
        columns={
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
    )
    missing = {"trade_date", *FIELDS} - set(frame.columns)
    if missing:
        raise RuntimeError(f"AKShare qfq rows omitted fields for {symbol}: {sorted(missing)}")
    result = frame.loc[:, ["trade_date", *FIELDS]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.set_index("trade_date").sort_index(kind="stable")
    for field in FIELDS:
        result[field] = pd.to_numeric(result[field], errors="raise")
    result["amount"] = result["amount"] / 1000.0
    result.index.name = "trade_date"
    if result.index.has_duplicates:
        raise RuntimeError(f"AKShare qfq rows contain duplicate dates for {symbol}")
    if len(result) < 3:
        raise RuntimeError(f"AKShare qfq window is too short for {symbol}")
    return result


def _fetch_qfq_frames(
    symbols: tuple[str, ...],
    *,
    start_date: date,
    end_date: date,
    workers: int,
) -> dict[str, pd.DataFrame]:
    import akshare as ak

    frames: dict[str, pd.DataFrame] = {}

    def fetch_one(symbol: str) -> tuple[str, pd.DataFrame]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                frame = cached_loader_fetch(
                    source="akshare_qfq",
                    symbol=symbol,
                    timeframe="1D",
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    fields=("__qe3_qfq_ohlcva_v1",),
                    fetch=lambda: _normalized_akshare_qfq(
                        ak,
                        symbol,
                        start_date,
                        end_date,
                    ),
                )
                if frame is None:
                    raise RuntimeError(f"AKShare returned no qfq rows for {symbol}")
                return symbol, frame
            except Exception as exc:  # noqa: BLE001 - retry stays explicit
                last_error = exc
                if attempt < 2:
                    time.sleep(1.0 + attempt)
        raise RuntimeError(f"AKShare qfq fetch failed for {symbol}") from last_error

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol, frame = future.result()
            frames[symbol] = frame
    if set(frames) != set(symbols):
        raise RuntimeError("AKShare qfq fetch did not cover all CSI300 symbols")
    return dict(sorted(frames.items()))


class _FrozenQfqLoader:
    name = "akshare"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def is_available(self) -> bool:
        return True

    def fetch_for_envelope(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str,
        fields: list[str] | None,
    ) -> dict[str, pd.DataFrame]:
        del start_date, end_date, interval, fields
        return {
            code: self._frames[code].copy()
            for code in codes
            if code in self._frames
        }


def _build_envelopes(
    frames: dict[str, pd.DataFrame],
    *,
    start_date: date,
    end_date: date,
    source_version: str,
) -> tuple[Any, ...]:
    symbols = tuple(frames)
    capability = LoaderCapability(
        source="akshare",
        version=source_version,
        instrument_types=("stock",),
        intervals=("1D",),
        adjustments=("qfq",),
        fields=FIELDS,
        field_units={
            "stock": {
                "open": "CNY/share",
                "high": "CNY/share",
                "low": "CNY/share",
                "close": "CNY/share",
                "volume": "lot_100_shares",
                "amount": "CNY_1000",
            }
        },
    )
    loader = _FrozenQfqLoader(frames)
    envelopes = []
    for offset in range(0, len(symbols), 100):
        shard_symbols = symbols[offset : offset + 100]
        request = DataFetchRequest(
            symbols=shard_symbols,
            instrument_types={symbol: "stock" for symbol in shard_symbols},
            start_date=start_date,
            end_date=end_date,
            adjustment="qfq",
            fields=FIELDS,
            requested_sources=("akshare",),
        )
        envelopes.append(
            fetch_data_envelope(
                request,
                loaders={"akshare": loader},
                capabilities={"akshare": capability},
            ).require_complete()
        )
    return tuple(envelopes)


def _write_json(path: Path, value: Any) -> str:
    payload = canonical_json(value).encode("utf-8")
    digest = canonical_sha256(value)
    if path.exists():
        raise RuntimeError(f"refusing to overwrite artifact: {path}")
    path.write_bytes(payload)
    path.chmod(0o600)
    return digest


def _cached_source_frame(path: Path, fetch: Any) -> pd.DataFrame:
    """Persist one exact upstream table so low-frequency calls are resumable."""

    if path.is_file():
        frame = pd.read_json(path, orient="table")
        if frame.empty:
            raise RuntimeError(f"cached source frame is empty: {path}")
        return frame
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = fetch()
    if frame is None or frame.empty:
        raise RuntimeError(f"upstream source returned no rows for {path.stem}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_json(
        temporary,
        orient="table",
        date_format="iso",
        force_ascii=False,
        index=False,
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    return frame


def _artifact_ref(
    file_name: str,
    value: Any,
    *,
    schema_version: str,
    record_count: int,
) -> Qe3ArtifactRef:
    return Qe3ArtifactRef(
        file_name=file_name,
        schema_version=schema_version,
        content_sha256=canonical_sha256(value),
        record_count=record_count,
    )


def materialize(args: argparse.Namespace) -> Path:
    load_dotenv(AGENT_DIR / ".env")
    os.environ["VIBE_TRADING_DATA_CACHE"] = "1"
    os.environ["VIBE_TRADING_DATA_CACHE_ROOT"] = str(
        AGENT_DIR / "runs" / "qe3-production" / "loader-cache"
    )
    import akshare as ak
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token or token == "your-tushare-token":
        raise RuntimeError("TUSHARE_TOKEN is not configured in agent/.env")
    captured_at = datetime.now(SHANGHAI)
    if captured_at.date() != args.as_of:
        raise RuntimeError("--as-of must equal the Shanghai capture date")
    if args.market_date > args.as_of or args.start_date >= args.market_date:
        raise RuntimeError("dates must satisfy start_date < market_date <= as_of")

    pro = ts.pro_api(token)
    source_cache = (
        AGENT_DIR
        / "runs"
        / "qe3-production"
        / "source-cache"
        / f"{args.as_of:%Y%m%d}"
    )
    constituents_df = _cached_source_frame(
        source_cache / "csindex-constituents.json",
        lambda: ak.index_stock_cons_csindex(symbol="000300"),
    )
    weights_df = _cached_source_frame(
        source_cache / "csindex-weights.json",
        lambda: ak.index_stock_cons_weight_csindex(symbol="000300"),
    )
    daily_basic_df = _cached_source_frame(
        source_cache / f"tushare-daily-basic-{args.market_date:%Y%m%d}.json",
        lambda: pro.daily_basic(
            trade_date=args.market_date.strftime("%Y%m%d"),
            fields="ts_code,trade_date,total_mv",
        ),
    )
    stock_basic_df = _cached_source_frame(
        source_cache / "tushare-stock-basic.json",
        lambda: pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,exchange,list_status,list_date,delist_date,industry",
        ),
    )
    if stock_basic_df is None or stock_basic_df.empty:
        raise RuntimeError("Tushare stock_basic returned no rows")
    if daily_basic_df is None or daily_basic_df.empty:
        raise RuntimeError("Tushare daily_basic returned no rows for market_date")

    source_version = f"akshare-{ak.__version__}"
    source_batch = build_csi300_csindex_source_batch(
        constituents_df.to_dict(orient="records"),
        weights_df.to_dict(orient="records"),
        stock_basic_df.to_dict(orient="records"),
        batch_id=f"csi300-{args.as_of.isoformat()}-production",
        source_version=source_version,
        as_of=args.as_of,
        captured_at=captured_at,
    )
    universe = materialize_csi300_csindex_universe(source_batch)
    symbols = source_batch.symbols
    frames = _fetch_qfq_frames(
        symbols,
        start_date=args.start_date,
        end_date=args.market_date,
        workers=args.workers,
    )
    envelopes = _build_envelopes(
        frames,
        start_date=args.start_date,
        end_date=args.market_date,
        source_version=f"akshare-qfq-{ak.__version__}",
    )
    data_bundle = build_qe3_data_snapshot_bundle(envelopes, as_of=args.as_of)
    price_volume = build_price_volume_snapshot_from_bundle(
        data_bundle,
        envelopes,
        window_days=args.price_window,
        snapshot_id=f"qe3-production-price-volume-{args.as_of:%Y%m%d}",
    )
    factors = build_factor_snapshot_from_price_volume(
        price_volume,
        snapshot_id=f"qe3-production-factors-{args.as_of:%Y%m%d}",
        source_version=f"akshare-qfq-{ak.__version__}",
        known_at=captured_at,
        factor_window_days=args.factor_window,
    )
    daily_rows = []
    for symbol, frame in frames.items():
        for trade_date, row in frame.tail(args.liquidity_window).iterrows():
            daily_rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": pd.Timestamp(trade_date).strftime("%Y%m%d"),
                    "amount": float(row["amount"]),
                }
            )
    business = build_tushare_business_feature_snapshot(
        stock_basic_df.to_dict(orient="records"),
        daily_basic_df.to_dict(orient="records"),
        daily_rows,
        symbols=symbols,
        snapshot_id=f"qe3-production-business-{args.as_of:%Y%m%d}",
        source_version=f"tushare-{ts.__version__}",
        as_of=args.as_of,
        market_trade_date=args.market_date,
        captured_at=captured_at,
        liquidity_window_days=args.liquidity_window,
    )
    validate_qe3_production_artifacts(
        source_batch,
        universe,
        data_bundle,
        business,
        factors,
        price_volume,
    )

    output = args.output_dir.resolve()
    if output.exists():
        raise RuntimeError(f"refusing to overwrite output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent)
    )
    try:
        for shard, envelope in zip(data_bundle.shards, envelopes, strict=True):
            write_offline_snapshot(envelope, temporary / shard.file_name)
        universe_snapshot = universe.snapshot(args.as_of)
        artifacts = (
            ("csi300-source.json", source_batch),
            ("universe-history.json", universe),
            ("universe-snapshot.json", universe_snapshot),
            ("data-snapshot-bundle.json", data_bundle),
            ("business-features.json", business),
            ("factor-features.json", factors),
            ("price-volume-features.json", price_volume),
        )
        for file_name, value in artifacts:
            _write_json(temporary / file_name, value)
        manifest = Qe3ProductionArtifactManifest(
            as_of=args.as_of,
            captured_at=captured_at,
            source_batch=_artifact_ref(
                "csi300-source.json",
                source_batch,
                schema_version=source_batch.schema_version,
                record_count=len(source_batch.constituents),
            ),
            universe_history=_artifact_ref(
                "universe-history.json",
                universe,
                schema_version=universe.schema_version,
                record_count=len(universe.instruments),
            ),
            universe_snapshot=_artifact_ref(
                "universe-snapshot.json",
                universe_snapshot,
                schema_version=universe_snapshot.schema_version,
                record_count=len(universe_snapshot.active_symbols),
            ),
            data_bundle=_artifact_ref(
                "data-snapshot-bundle.json",
                data_bundle,
                schema_version=data_bundle.schema_version,
                record_count=len(data_bundle.symbols),
            ),
            business_features=_artifact_ref(
                "business-features.json",
                business,
                schema_version=business.schema_version,
                record_count=len(business.records),
            ),
            factor_features=_artifact_ref(
                "factor-features.json",
                factors,
                schema_version=factors.schema_version,
                record_count=len(factors.records),
            ),
            price_volume_features=_artifact_ref(
                "price-volume-features.json",
                price_volume,
                schema_version=price_volume.schema_version,
                record_count=len(price_volume.records),
            ),
        )
        _write_json(temporary / "manifest.json", manifest)
        temporary.rename(output)
    except Exception:
        raise
    return output


def parse_args() -> argparse.Namespace:
    today = datetime.now(SHANGHAI).date()
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=_parse_date, default=today)
    parser.add_argument("--market-date", type=_parse_date, required=True)
    parser.add_argument("--start-date", type=_parse_date, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--price-window", type=int, default=60)
    parser.add_argument("--factor-window", type=int, default=20)
    parser.add_argument("--liquidity-window", type=int, default=20)
    return parser.parse_args()


if __name__ == "__main__":
    destination = materialize(parse_args())
    print(destination)
