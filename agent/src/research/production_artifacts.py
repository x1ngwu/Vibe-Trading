"""QE3 production artifact contracts and deterministic materialization helpers."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from statistics import pstdev
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .business_similarity import BusinessFeatureSnapshot
from .contracts import DataSnapshotRef, canonical_sha256
from .factor_similarity import (
    FactorFeatureRecord,
    FactorFeatureSnapshot,
    FactorFeatureValue,
)
from .price_volume_similarity import (
    PriceVolumeFeatureSnapshot,
    build_price_volume_feature_snapshot_from_envelope,
)
from .universe import (
    PointInTimeUniverse,
    UniverseError,
    UniverseInstrument,
    UniverseMembership,
)
from .universe_source import (
    CSI300_CANONICAL_INDEX_SYMBOL,
    CSI300_EXPECTED_CONSTITUENTS,
)

CSI300_CSINDEX_SOURCE_SCHEMA = "vibe.csi300-csindex-source-batch.v1"
QE3_DATA_SNAPSHOT_BUNDLE_SCHEMA = "vibe.qe3-data-snapshot-bundle.v1"
QE3_PRODUCTION_MANIFEST_SCHEMA = "vibe.qe3-production-manifest.v1"


class ProductionArtifactError(ValueError):
    """Raised when live-source artifacts cannot form one reproducible bundle."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Csi300CsindexConstituent(_StrictModel):
    """One official CSI constituent joined to lifecycle metadata and weight."""

    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ)$")
    name: str = Field(min_length=1, max_length=128)
    exchange: Literal["SSE", "SZSE"]
    membership_date: date
    weight_date: date
    weight: Decimal = Field(gt=Decimal("0"), le=Decimal("100"))
    listing_date: date
    delisting_date: date | None = None

    @model_validator(mode="after")
    def validate_constituent(self) -> "Csi300CsindexConstituent":
        expected = "SSE" if self.symbol.endswith(".SH") else "SZSE"
        if self.exchange != expected:
            raise ValueError("exchange does not match constituent symbol")
        if self.listing_date > self.membership_date:
            raise ValueError("constituent was not listed by membership_date")
        if self.weight_date > self.membership_date:
            raise ValueError("weight_date must not exceed membership_date")
        if self.delisting_date is not None and self.delisting_date < self.listing_date:
            raise ValueError("delisting_date must not precede listing_date")
        return self


class Csi300CsindexSourceBatch(_StrictModel):
    """Complete current CSI300 capture from the official CSI website."""

    schema_version: Literal["vibe.csi300-csindex-source-batch.v1"] = (
        CSI300_CSINDEX_SOURCE_SCHEMA
    )
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    source: Literal["csindex"] = "csindex"
    source_version: str = Field(min_length=1, max_length=64)
    index_symbol: Literal["000300.SH"] = CSI300_CANONICAL_INDEX_SYMBOL
    as_of: date
    captured_at: AwareDatetime
    constituents: tuple[Csi300CsindexConstituent, ...]

    @field_validator("constituents")
    @classmethod
    def normalize_constituents(
        cls,
        values: tuple[Csi300CsindexConstituent, ...],
    ) -> tuple[Csi300CsindexConstituent, ...]:
        return tuple(sorted(values, key=lambda item: item.symbol))

    @model_validator(mode="after")
    def validate_batch(self) -> "Csi300CsindexSourceBatch":
        if len(self.constituents) != CSI300_EXPECTED_CONSTITUENTS:
            raise ValueError("CSI300 source batch must contain exactly 300 constituents")
        symbols = [item.symbol for item in self.constituents]
        if len(symbols) != len(set(symbols)):
            raise ValueError("CSI300 source batch contains duplicate symbols")
        membership_dates = {item.membership_date for item in self.constituents}
        weight_dates = {item.weight_date for item in self.constituents}
        if len(membership_dates) != 1 or len(weight_dates) != 1:
            raise ValueError("CSI300 source rows must use one membership and weight date")
        membership_date = next(iter(membership_dates))
        capture_date = self.captured_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date()
        if membership_date > self.as_of:
            raise ValueError("membership_date must not exceed as_of")
        if capture_date < membership_date or capture_date > self.as_of:
            raise ValueError("captured_at must fall between membership_date and as_of")
        total_weight = sum(
            (item.weight for item in self.constituents),
            start=Decimal("0"),
        )
        if not Decimal("99") <= total_weight <= Decimal("101"):
            raise ValueError(f"CSI300 source weights must close near 100, got {total_weight}")
        if any(
            item.delisting_date is not None and item.delisting_date <= self.as_of
            for item in self.constituents
        ):
            raise ValueError("CSI300 source contains a security delisted by as_of")
        return self

    @property
    def source_content_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.constituents)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProductionArtifactError(f"source field {field} must be non-empty text")
    return value.strip()


def _optional_date(value: Any, field: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = str(value).strip().replace("-", "")
    if len(normalized) == 8 and normalized.isdigit():
        try:
            return date(
                int(normalized[:4]),
                int(normalized[4:6]),
                int(normalized[6:]),
            )
        except ValueError as exc:
            raise ProductionArtifactError(f"source field {field} is not a date") from exc
    raise ProductionArtifactError(f"source field {field} must be YYYYMMDD")


def _csindex_symbol(row: Mapping[str, Any]) -> tuple[str, Literal["SSE", "SZSE"]]:
    code = _required_text(row, "成分券代码").zfill(6)
    exchange_name = _required_text(row, "交易所")
    if "上海" in exchange_name:
        return f"{code}.SH", "SSE"
    if "深圳" in exchange_name:
        return f"{code}.SZ", "SZSE"
    raise ProductionArtifactError(f"unsupported CSI exchange: {exchange_name}")


def build_csi300_csindex_source_batch(
    constituent_rows: Sequence[Mapping[str, Any]],
    weight_rows: Sequence[Mapping[str, Any]],
    stock_basic_rows: Sequence[Mapping[str, Any]],
    *,
    batch_id: str,
    source_version: str,
    as_of: date,
    captured_at: datetime,
) -> Csi300CsindexSourceBatch:
    """Join exact official CSI files to Tushare lifecycle metadata."""

    weights: dict[str, tuple[date, Decimal]] = {}
    for row in weight_rows:
        symbol, _exchange = _csindex_symbol(row)
        if symbol in weights:
            raise ProductionArtifactError(f"duplicate CSI weight row: {symbol}")
        weight_date = _optional_date(row.get("日期"), "日期")
        if weight_date is None:
            raise ProductionArtifactError("CSI weight row omitted 日期")
        try:
            weight = Decimal(str(row.get("权重")))
        except Exception as exc:
            raise ProductionArtifactError("CSI 权重 must be numeric") from exc
        weights[symbol] = (weight_date, weight)

    stock_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in stock_basic_rows:
        symbol = _required_text(row, "ts_code")
        if symbol in stock_by_symbol:
            raise ProductionArtifactError(f"duplicate stock_basic row: {symbol}")
        stock_by_symbol[symbol] = row

    constituents: list[Csi300CsindexConstituent] = []
    seen: set[str] = set()
    for row in constituent_rows:
        symbol, exchange = _csindex_symbol(row)
        if symbol in seen:
            raise ProductionArtifactError(f"duplicate CSI constituent row: {symbol}")
        seen.add(symbol)
        if symbol not in weights:
            raise ProductionArtifactError(f"CSI weight missing for {symbol}")
        stock = stock_by_symbol.get(symbol)
        if stock is None:
            raise ProductionArtifactError(f"stock_basic metadata missing for {symbol}")
        if _required_text(stock, "exchange") != exchange:
            raise ProductionArtifactError(f"stock_basic exchange mismatch for {symbol}")
        if _required_text(stock, "list_status") != "L":
            raise ProductionArtifactError(f"CSI constituent is not listed: {symbol}")
        membership_date = _optional_date(row.get("日期"), "日期")
        listing_date = _optional_date(stock.get("list_date"), "list_date")
        if membership_date is None or listing_date is None:
            raise ProductionArtifactError("CSI membership/listing date is missing")
        weight_date, weight = weights[symbol]
        constituents.append(
            Csi300CsindexConstituent(
                symbol=symbol,
                name=_required_text(row, "成分券名称"),
                exchange=exchange,
                membership_date=membership_date,
                weight_date=weight_date,
                weight=weight,
                listing_date=listing_date,
                delisting_date=_optional_date(stock.get("delist_date"), "delist_date"),
            )
        )
    if set(weights) != seen:
        raise ProductionArtifactError("CSI constituent and weight symbol sets differ")
    return Csi300CsindexSourceBatch(
        batch_id=batch_id,
        source_version=source_version,
        as_of=as_of,
        captured_at=captured_at,
        constituents=tuple(constituents),
    )


def materialize_csi300_csindex_universe(
    batch: Csi300CsindexSourceBatch,
) -> PointInTimeUniverse:
    """Convert one complete official CSI capture into the existing universe DAG."""

    source_hash = batch.source_content_sha256
    instruments = tuple(
        UniverseInstrument(
            security_id=f"TS:{item.symbol}",
            symbol=item.symbol,
            instrument_type="stock",
            listing_date=item.listing_date,
            delisting_date=item.delisting_date,
            primary_listing=True,
            known_at=batch.captured_at,
        )
        for item in batch.constituents
    )
    memberships = tuple(
        UniverseMembership(
            source_row_id=(
                f"csindex:000300:{item.membership_date:%Y%m%d}:{item.symbol}"
            ),
            symbol=item.symbol,
            effective_from=item.membership_date,
            effective_to=batch.as_of,
            known_at=batch.captured_at,
        )
        for item in batch.constituents
    )
    universe = PointInTimeUniverse(
        fixture_id=f"csi300-{batch.as_of.isoformat()}-csindex-{source_hash[:16]}",
        universe_id="csi300",
        index_symbol=CSI300_CANONICAL_INDEX_SYMBOL,
        source="csindex",
        source_version=f"{batch.source_version}+sha256.{source_hash}",
        instruments=instruments,
        memberships=memberships,
    )
    if len(universe.snapshot(batch.as_of).active_symbols) != 300:
        raise UniverseError("materialized CSI300 snapshot is not exactly 300 members")
    return universe


class Qe3DataShardRef(_StrictModel):
    file_name: str = Field(pattern=r"^data-snapshot-[0-9]{3}\.json$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbols: tuple[str, ...] = Field(min_length=1, max_length=128)


class Qe3DataSnapshotBundle(_StrictModel):
    """One 300-name research snapshot composed from bounded QE2 snapshots."""

    schema_version: Literal["vibe.qe3-data-snapshot-bundle.v1"] = (
        QE3_DATA_SNAPSHOT_BUNDLE_SCHEMA
    )
    as_of: date
    start_date: date
    end_date: date
    adjustment: Literal["qfq"] = "qfq"
    fields: tuple[str, ...]
    requested_sources: tuple[str, ...]
    actual_sources: dict[str, str]
    symbols: tuple[str, ...]
    shards: tuple[Qe3DataShardRef, ...]

    @model_validator(mode="after")
    def validate_bundle(self) -> "Qe3DataSnapshotBundle":
        if self.start_date > self.end_date or self.end_date > self.as_of:
            raise ValueError("bundle dates must satisfy start_date <= end_date <= as_of")
        if self.fields != ("open", "high", "low", "close", "volume", "amount"):
            raise ValueError("QE3 bundle fields must be canonical OHLCVA")
        if len(self.symbols) != 300 or tuple(sorted(self.symbols)) != self.symbols:
            raise ValueError("QE3 bundle must contain 300 sorted symbols")
        if set(self.actual_sources) != set(self.symbols):
            raise ValueError("actual_sources must cover every bundle symbol")
        if len(self.shards) != 3:
            raise ValueError("QE3 CSI300 bundle must contain exactly three shards")
        shard_symbols = tuple(symbol for shard in self.shards for symbol in shard.symbols)
        if shard_symbols != self.symbols:
            raise ValueError("shards must partition bundle symbols in stable order")
        if len({item.file_name for item in self.shards}) != len(self.shards):
            raise ValueError("shard file names must be unique")
        return self

    @property
    def bundle_sha256(self) -> str:
        return canonical_sha256(self)

    def snapshot_ref(self) -> DataSnapshotRef:
        return DataSnapshotRef(
            snapshot_sha256=self.bundle_sha256,
            as_of=self.as_of,
            start_date=self.start_date,
            end_date=self.end_date,
            adjustment=self.adjustment,
            symbols=self.symbols,
            fields=self.fields,
            requested_sources=self.requested_sources,
            actual_sources=self.actual_sources,
        )


def build_qe3_data_snapshot_bundle(
    envelopes: Sequence[Any],
    *,
    as_of: date | None = None,
) -> Qe3DataSnapshotBundle:
    """Bind three complete, disjoint QE2 envelopes into one CSI300 identity."""

    ordered = tuple(sorted(envelopes, key=lambda item: item.request.symbols[0]))
    if len(ordered) != 3:
        raise ProductionArtifactError("QE3 production data requires exactly three shards")
    first = ordered[0].request
    symbols: list[str] = []
    actual_sources: dict[str, str] = {}
    shards: list[Qe3DataShardRef] = []
    for index, envelope in enumerate(ordered, start=1):
        envelope.require_complete()
        request = envelope.request
        if (
            request.start_date != first.start_date
            or request.end_date != first.end_date
            or request.adjustment != "qfq"
            or request.fields != first.fields
            or request.requested_sources != first.requested_sources
        ):
            raise ProductionArtifactError("QE3 data shards have inconsistent requests")
        if tuple(sorted(request.symbols)) != request.symbols:
            raise ProductionArtifactError("QE3 data shard symbols must be sorted")
        if set(symbols) & set(request.symbols):
            raise ProductionArtifactError("QE3 data shards overlap")
        symbols.extend(request.symbols)
        actual_sources.update(envelope.manifest.actual_sources)
        shards.append(
            Qe3DataShardRef(
                file_name=f"data-snapshot-{index:03d}.json",
                snapshot_sha256=envelope.manifest.snapshot_sha256,
                symbols=request.symbols,
            )
        )
    return Qe3DataSnapshotBundle(
        as_of=as_of or first.end_date,
        start_date=first.start_date,
        end_date=first.end_date,
        fields=first.fields,
        requested_sources=first.requested_sources,
        actual_sources=actual_sources,
        symbols=tuple(symbols),
        shards=tuple(shards),
    )


def build_price_volume_snapshot_from_bundle(
    bundle: Qe3DataSnapshotBundle,
    envelopes: Sequence[Any],
    *,
    window_days: int,
    snapshot_id: str,
) -> PriceVolumeFeatureSnapshot:
    """Reuse the QE3 single-envelope adapter for each verified QE2 shard."""

    by_hash = {
        envelope.manifest.snapshot_sha256: envelope for envelope in envelopes
    }
    records = []
    for index, shard in enumerate(bundle.shards, start=1):
        envelope = by_hash.get(shard.snapshot_sha256)
        if envelope is None or envelope.request.symbols != shard.symbols:
            raise ProductionArtifactError("bundle shard envelope is missing or mismatched")
        partial = build_price_volume_feature_snapshot_from_envelope(
            envelope.snapshot_ref(),
            envelope,
            window_days=window_days,
            snapshot_id=f"{snapshot_id}-shard-{index}",
        )
        records.extend(partial.records)
    return PriceVolumeFeatureSnapshot(
        snapshot_id=snapshot_id,
        data_snapshot_sha256=bundle.bundle_sha256,
        as_of=bundle.as_of,
        window_days=window_days,
        records=tuple(records),
    )


def build_factor_snapshot_from_price_volume(
    features: PriceVolumeFeatureSnapshot,
    *,
    snapshot_id: str,
    source_version: str,
    known_at: datetime,
    factor_window_days: int = 20,
) -> FactorFeatureSnapshot:
    """Derive a minimal auditable factor vector from the same qfq paths."""

    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ProductionArtifactError("known_at must be timezone-aware")
    if known_at.astimezone(ZoneInfo("Asia/Shanghai")).date() > features.as_of:
        raise ProductionArtifactError("factor known_at exceeds feature as_of")
    records: list[FactorFeatureRecord] = []
    for record in features.records:
        observations = record.observations[-factor_window_days:]
        if len(observations) < 3:
            raise ProductionArtifactError(
                f"factor window has fewer than three rows for {record.symbol}"
            )
        closes = [item.close for item in observations]
        amounts = [item.amount for item in observations]
        returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
        ]
        peak = closes[0]
        max_drawdown = 0.0
        for close in closes:
            peak = max(peak, close)
            max_drawdown = min(max_drawdown, close / peak - 1.0)
        factors = {
            "drawdown_20d": max_drawdown,
            "momentum_20d": closes[-1] / closes[0] - 1.0,
            "turnover_change_20d": (
                amounts[-1] / amounts[0] - 1.0 if amounts[0] > 0.0 else 0.0
            ),
            "volatility_20d": pstdev(returns),
        }
        records.append(
            FactorFeatureRecord(
                symbol=record.symbol,
                values=tuple(
                    FactorFeatureValue(
                        factor_id=factor_id,
                        value=value,
                        source="akshare",
                        source_version=source_version,
                        known_at=known_at,
                        source_fields=("qfq.close", "qfq.amount"),
                    )
                    for factor_id, value in factors.items()
                ),
            )
        )
    return FactorFeatureSnapshot(
        snapshot_id=snapshot_id,
        as_of=features.as_of,
        records=tuple(records),
    )


class Qe3ArtifactRef(_StrictModel):
    file_name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}\.json$")
    schema_version: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=1)


class Qe3ProductionArtifactManifest(_StrictModel):
    schema_version: Literal["vibe.qe3-production-manifest.v1"] = (
        QE3_PRODUCTION_MANIFEST_SCHEMA
    )
    as_of: date
    captured_at: AwareDatetime
    symbol_count: Literal[300] = 300
    source_batch: Qe3ArtifactRef
    universe_history: Qe3ArtifactRef
    universe_snapshot: Qe3ArtifactRef
    data_bundle: Qe3ArtifactRef
    business_features: Qe3ArtifactRef
    factor_features: Qe3ArtifactRef
    price_volume_features: Qe3ArtifactRef

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


def validate_qe3_production_artifacts(
    source_batch: Csi300CsindexSourceBatch,
    universe: PointInTimeUniverse,
    data_bundle: Qe3DataSnapshotBundle,
    business: BusinessFeatureSnapshot,
    factors: FactorFeatureSnapshot,
    price_volume: PriceVolumeFeatureSnapshot,
) -> None:
    """Fail closed unless all production artifacts describe the same 300 names."""

    symbols = set(source_batch.symbols)
    universe_symbols = set(universe.snapshot(source_batch.as_of).active_symbols)
    feature_sets = (
        set(data_bundle.symbols),
        {item.symbol for item in business.records},
        {item.symbol for item in factors.records},
        {item.symbol for item in price_volume.records},
    )
    if any(items != symbols for items in (universe_symbols, *feature_sets)):
        raise ProductionArtifactError("QE3 production artifact symbol sets differ")
    if not (
        source_batch.as_of
        == data_bundle.as_of
        == business.as_of
        == factors.as_of
        == price_volume.as_of
    ):
        raise ProductionArtifactError("QE3 production artifact as_of values differ")
    if price_volume.data_snapshot_sha256 != data_bundle.bundle_sha256:
        raise ProductionArtifactError("price-volume features do not bind data bundle")
