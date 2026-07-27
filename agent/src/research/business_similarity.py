"""Content-bound business features and deterministic QE3 similarity scoring."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .contracts import (
    ChannelWeights,
    DataSnapshotRef,
    PeerSet,
    ResearchObject,
    ResearchSpec,
    SimilarityRun,
    StockCandidate,
    canonical_sha256,
    create_research_object,
)

BUSINESS_FEATURE_SNAPSHOT_SCHEMA = "vibe.business-feature-snapshot.v1"

BusinessDimension = Literal[
    "industry",
    "market_cap",
    "liquidity",
    "listing_age",
]


class BusinessSimilarityError(ValueError):
    """Raised when structured similarity inputs are incomplete or unbound."""


class _StrictBusinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BusinessFieldProvenance(_StrictBusinessModel):
    """Exact source and knowledge time for one structured field."""

    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source_version: str = Field(min_length=1, max_length=128)
    known_at: AwareDatetime
    source_fields: tuple[str, ...] = Field(min_length=1)

    @field_validator("source_fields")
    @classmethod
    def normalize_source_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result = tuple(sorted(values))
        if len(result) != len(set(result)):
            raise ValueError("source_fields must not contain duplicates")
        if any(
            not value
            or len(value) > 128
            or not value[0].isalnum()
            for value in result
        ):
            raise ValueError("source_fields contain an invalid identifier")
        return result


class BusinessFeatureRecord(_StrictBusinessModel):
    """Point-in-time business fields for one canonical stock symbol."""

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    industry: str | None = Field(default=None, max_length=128)
    market_cap_cny: float | None = Field(default=None, gt=0.0)
    average_daily_turnover_cny: float | None = Field(default=None, gt=0.0)
    liquidity_observation_count: int | None = Field(default=None, ge=1)
    listing_date: date | None = None
    provenance: dict[BusinessDimension, BusinessFieldProvenance]

    @field_validator("industry")
    @classmethod
    def normalize_industry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("industry must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_populated_fields(self) -> "BusinessFeatureRecord":
        populated: set[str] = set()
        if self.industry is not None:
            populated.add("industry")
        if self.market_cap_cny is not None:
            populated.add("market_cap")
        if self.average_daily_turnover_cny is not None:
            populated.add("liquidity")
        if self.listing_date is not None:
            populated.add("listing_age")
        if not populated:
            raise ValueError("business feature record must contain at least one field")
        if set(self.provenance) != populated:
            raise ValueError("provenance must cover populated business fields exactly")
        has_liquidity = self.average_daily_turnover_cny is not None
        has_observations = self.liquidity_observation_count is not None
        if has_liquidity != has_observations:
            raise ValueError(
                "liquidity value and liquidity_observation_count must appear together"
            )
        return self


class BusinessFeatureSnapshot(_StrictBusinessModel):
    """Order-normalized structured features visible at one research cutoff."""

    schema_version: Literal["vibe.business-feature-snapshot.v1"] = (
        BUSINESS_FEATURE_SNAPSHOT_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    as_of: date
    currency: Literal["CNY"] = "CNY"
    cutoff_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    liquidity_window_days: int = Field(ge=1, le=252)
    records: tuple[BusinessFeatureRecord, ...] = Field(min_length=2)

    @field_validator("records")
    @classmethod
    def normalize_record_order(
        cls,
        values: tuple[BusinessFeatureRecord, ...],
    ) -> tuple[BusinessFeatureRecord, ...]:
        return tuple(sorted(values, key=lambda item: item.symbol))

    @model_validator(mode="after")
    def validate_point_in_time_snapshot(self) -> "BusinessFeatureSnapshot":
        symbols = [item.symbol for item in self.records]
        if len(symbols) != len(set(symbols)):
            raise ValueError("business feature symbols must be unique")
        timezone = ZoneInfo(self.cutoff_timezone)
        for record in self.records:
            if record.listing_date is not None and record.listing_date > self.as_of:
                raise ValueError(
                    f"listing_date for {record.symbol} must not exceed as_of"
                )
            if (
                record.liquidity_observation_count is not None
                and record.liquidity_observation_count > self.liquidity_window_days
            ):
                raise ValueError(
                    f"liquidity observations exceed window for {record.symbol}"
                )
            for dimension, provenance in record.provenance.items():
                if provenance.known_at.astimezone(timezone).date() > self.as_of:
                    raise ValueError(
                        f"future-known {dimension} for {record.symbol} exceeds as_of"
                    )
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)


def _required_text(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise BusinessSimilarityError(
            f"Tushare field {field_name} must be a non-empty string"
        )
    return value.strip()


def _source_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        normalized = value.strip().replace("-", "")
        if len(normalized) == 8 and normalized.isdigit():
            try:
                return date(
                    int(normalized[:4]),
                    int(normalized[4:6]),
                    int(normalized[6:]),
                )
            except ValueError as exc:
                raise BusinessSimilarityError(
                    f"Tushare field {field_name} contains an invalid date"
                ) from exc
    raise BusinessSimilarityError(f"Tushare field {field_name} must be YYYYMMDD")


def _optional_positive_number(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BusinessSimilarityError(
            f"Tushare field {field_name} must be numeric"
        ) from exc
    if not math.isfinite(number) or number <= 0.0:
        raise BusinessSimilarityError(
            f"Tushare field {field_name} must be finite and positive"
        )
    return number


def build_tushare_business_feature_snapshot(
    stock_basic_rows: Sequence[Mapping[str, Any]],
    daily_basic_rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    snapshot_id: str,
    source_version: str,
    as_of: date,
    market_trade_date: date,
    captured_at: datetime,
    liquidity_window_days: int,
) -> BusinessFeatureSnapshot:
    """Materialize exact Tushare exports with explicit documented unit scaling."""

    requested_symbols = tuple(sorted(symbols))
    if len(requested_symbols) < 2 or len(requested_symbols) != len(
        set(requested_symbols)
    ):
        raise BusinessSimilarityError(
            "symbols must contain at least two unique identifiers"
        )
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise BusinessSimilarityError("captured_at must be timezone-aware")
    capture_date = captured_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if market_trade_date > as_of:
        raise BusinessSimilarityError("market_trade_date must not exceed as_of")
    if capture_date < market_trade_date or capture_date > as_of:
        raise BusinessSimilarityError(
            "captured_at must fall between market_trade_date and as_of"
        )

    stock_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in stock_basic_rows:
        symbol = _required_text(row, "ts_code")
        if symbol in stock_by_symbol:
            raise BusinessSimilarityError(f"duplicate stock_basic ts_code: {symbol}")
        stock_by_symbol[symbol] = row

    cap_by_symbol: dict[str, float] = {}
    seen_cap_symbols: set[str] = set()
    for row in daily_basic_rows:
        symbol = _required_text(row, "ts_code")
        trade_date = _source_date(row.get("trade_date"), "trade_date")
        if trade_date != market_trade_date:
            raise BusinessSimilarityError(
                "all daily_basic rows must match market_trade_date"
            )
        if symbol in seen_cap_symbols:
            raise BusinessSimilarityError(f"duplicate daily_basic ts_code: {symbol}")
        seen_cap_symbols.add(symbol)
        total_mv = _optional_positive_number(row.get("total_mv"), "total_mv")
        if total_mv is not None:
            cap_by_symbol[symbol] = total_mv * 10_000.0

    amounts_by_symbol: dict[str, list[float]] = {}
    seen_daily_rows: set[tuple[str, date]] = set()
    for row in daily_rows:
        symbol = _required_text(row, "ts_code")
        trade_date = _source_date(row.get("trade_date"), "trade_date")
        if trade_date > market_trade_date:
            raise BusinessSimilarityError(
                "daily liquidity rows must not exceed market_trade_date"
            )
        row_key = (symbol, trade_date)
        if row_key in seen_daily_rows:
            raise BusinessSimilarityError(
                f"duplicate daily liquidity row: {symbol}@{trade_date}"
            )
        seen_daily_rows.add(row_key)
        amount = _optional_positive_number(row.get("amount"), "amount")
        if amount is not None:
            amounts_by_symbol.setdefault(symbol, []).append(amount * 1_000.0)

    provenance_fields = {
        "industry": ("stock_basic.industry",),
        "market_cap": ("daily_basic.total_mv",),
        "liquidity": ("daily.amount",),
        "listing_age": ("stock_basic.list_date",),
    }

    def provenance(dimension: BusinessDimension) -> BusinessFieldProvenance:
        return BusinessFieldProvenance(
            source="tushare",
            source_version=source_version,
            known_at=captured_at,
            source_fields=provenance_fields[dimension],
        )

    records: list[BusinessFeatureRecord] = []
    for symbol in requested_symbols:
        stock_row = stock_by_symbol.get(symbol)
        if stock_row is not None and _required_text(stock_row, "list_status") != "L":
            raise BusinessSimilarityError(
                f"stock_basic list_status must be L for {symbol}"
            )
        industry: str | None = None
        listing_date: date | None = None
        if stock_row is not None:
            raw_industry = stock_row.get("industry")
            if isinstance(raw_industry, str) and raw_industry.strip():
                industry = raw_industry.strip()
            raw_listing_date = stock_row.get("list_date")
            if raw_listing_date not in (None, ""):
                listing_date = _source_date(raw_listing_date, "list_date")

        amounts = amounts_by_symbol.get(symbol, [])
        if len(amounts) > liquidity_window_days:
            raise BusinessSimilarityError(
                f"liquidity observations exceed window for {symbol}"
            )
        turnover = sum(amounts) / len(amounts) if amounts else None
        market_cap = cap_by_symbol.get(symbol)
        field_provenance: dict[BusinessDimension, BusinessFieldProvenance] = {}
        if industry is not None:
            field_provenance["industry"] = provenance("industry")
        if market_cap is not None:
            field_provenance["market_cap"] = provenance("market_cap")
        if turnover is not None:
            field_provenance["liquidity"] = provenance("liquidity")
        if listing_date is not None:
            field_provenance["listing_age"] = provenance("listing_age")
        if field_provenance:
            records.append(
                BusinessFeatureRecord(
                    symbol=symbol,
                    industry=industry,
                    market_cap_cny=market_cap,
                    average_daily_turnover_cny=turnover,
                    liquidity_observation_count=len(amounts) if amounts else None,
                    listing_date=listing_date,
                    provenance=field_provenance,
                )
            )

    return BusinessFeatureSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        liquidity_window_days=liquidity_window_days,
        records=tuple(records),
    )


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    return any(part.is_symlink() for part in (candidate, *candidate.parents))


def load_business_feature_snapshot(path: Path) -> BusinessFeatureSnapshot:
    """Load a strict local structured snapshot without network access."""

    snapshot_path = Path(path)
    if _has_symlink_component(snapshot_path) or not snapshot_path.is_file():
        raise BusinessSimilarityError(
            "business feature snapshot must be a regular, non-symlink file"
        )
    try:
        return BusinessFeatureSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise BusinessSimilarityError(
            f"invalid business feature snapshot: {exc}"
        ) from exc


def _require_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: BusinessFeatureSnapshot,
) -> tuple[ResearchSpec, DataSnapshotRef, PeerSet]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload,
        ResearchSpec,
    ):
        raise BusinessSimilarityError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload,
        DataSnapshotRef,
    ):
        raise BusinessSimilarityError(
            "data_snapshot must be a data_snapshot_ref object"
        )
    if peer_set.object_type != "peer_set" or not isinstance(
        peer_set.payload,
        PeerSet,
    ):
        raise BusinessSimilarityError("peer_set must be a peer_set object")
    if len({research.owner_scope, data_snapshot.owner_scope, peer_set.owner_scope}) != 1:
        raise BusinessSimilarityError("similarity inputs cross owner_scope boundaries")
    if research.ref() not in data_snapshot.parent_refs:
        raise BusinessSimilarityError(
            "data_snapshot does not descend from the research_spec"
        )
    if research.ref() not in peer_set.parent_refs or data_snapshot.ref() not in peer_set.parent_refs:
        raise BusinessSimilarityError(
            "peer_set does not descend from research and data snapshot"
        )

    spec = research.payload
    snapshot_ref = data_snapshot.payload
    peers = peer_set.payload
    if peers.research_spec_ref != research.ref() or peers.data_snapshot_ref != data_snapshot.ref():
        raise BusinessSimilarityError("peer_set payload references do not match inputs")
    if features.as_of != spec.as_of or snapshot_ref.as_of != spec.as_of:
        raise BusinessSimilarityError(
            "business features, research, and data snapshot must share as_of"
        )
    if not spec.peer_dimensions:
        raise BusinessSimilarityError("research peer_dimensions must not be empty")
    required_snapshot_symbols = {peers.target_symbol, *peers.members}
    if required_snapshot_symbols - set(snapshot_ref.symbols):
        raise BusinessSimilarityError(
            "data snapshot does not cover target and all peer members"
        )
    return spec, snapshot_ref, peers


def _feature_value(
    record: BusinessFeatureRecord,
    dimension: BusinessDimension,
    *,
    as_of: date,
) -> str | float | None:
    if dimension == "industry":
        return record.industry
    if dimension == "market_cap":
        return record.market_cap_cny
    if dimension == "liquidity":
        return record.average_daily_turnover_cny
    if record.listing_date is None:
        return None
    return float((as_of - record.listing_date).days + 1)


def _dimension_score(
    dimension: BusinessDimension,
    target_value: str | float,
    candidate_value: str | float,
) -> tuple[float, str]:
    if dimension == "industry":
        score = 1.0 if candidate_value == target_value else 0.0
        relation = "match" if score == 1.0 else "gap"
        detail = (
            f"business_{relation}:industry:target={target_value};"
            f"candidate={candidate_value};score={score:.6f}"
        )
        return score, detail

    target_number = float(target_value)
    candidate_number = float(candidate_value)
    score = round(
        min(target_number, candidate_number) / max(target_number, candidate_number),
        12,
    )
    detail = (
        f"business_similarity:{dimension}:ratio={score:.6f};"
        f"target={target_number:.6g};candidate={candidate_number:.6g}"
    )
    return score, detail


def _missing_side(
    target_value: str | float | None,
    candidate_value: str | float | None,
) -> str:
    if target_value is None and candidate_value is None:
        return "target_and_candidate"
    if target_value is None:
        return "target"
    return "candidate"


def _source_evidence(
    dimension: BusinessDimension,
    target: BusinessFeatureRecord,
    candidate: BusinessFeatureRecord,
) -> str:
    target_source = target.provenance[dimension]
    candidate_source = candidate.provenance[dimension]
    return (
        f"business_source:{dimension}:"
        f"target={target_source.source}@{target_source.source_version};"
        f"candidate={candidate_source.source}@{candidate_source.source_version}"
    )


def build_business_similarity(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: BusinessFeatureSnapshot,
    *,
    top_n: int,
    min_coverage: float,
) -> SimilarityRun:
    """Rank peer members using only requested, jointly available business fields."""

    spec, _snapshot_ref, peers = _require_inputs(
        research,
        data_snapshot,
        peer_set,
        features,
    )
    if not 1 <= top_n <= 500:
        raise BusinessSimilarityError("top_n must be between 1 and 500")
    if not 0.0 < min_coverage <= 1.0:
        raise BusinessSimilarityError("min_coverage must be in (0, 1]")
    feature_snapshot_sha256 = features.snapshot_sha256

    records = {item.symbol: item for item in features.records}
    target = records.get(peers.target_symbol)
    if target is None:
        raise BusinessSimilarityError("target business feature record is missing")
    dimensions = tuple(spec.peer_dimensions)
    target_available = [
        dimension
        for dimension in dimensions
        if _feature_value(target, dimension, as_of=spec.as_of) is not None
    ]
    if not target_available:
        raise BusinessSimilarityError(
            "target has no requested business dimensions available"
        )

    excluded = {
        symbol: tuple(reasons)
        for symbol, reasons in sorted(peers.excluded_reasons.items())
    }
    scored: list[dict[str, object]] = []
    for symbol in sorted(peers.members):
        candidate = records.get(symbol)
        if candidate is None:
            excluded[symbol] = ("business_feature_record_missing",)
            continue

        available_scores: list[tuple[BusinessDimension, float, str]] = []
        missing: list[str] = []
        for dimension in dimensions:
            target_value = _feature_value(target, dimension, as_of=spec.as_of)
            candidate_value = _feature_value(
                candidate,
                dimension,
                as_of=spec.as_of,
            )
            if target_value is None or candidate_value is None:
                missing.append(
                    f"missing_business_dimension:{dimension}:"
                    f"{_missing_side(target_value, candidate_value)}"
                )
                continue
            score, detail = _dimension_score(
                dimension,
                target_value,
                candidate_value,
            )
            available_scores.append((dimension, score, detail))

        coverage = len(available_scores) / len(dimensions)
        if coverage < min_coverage:
            excluded[symbol] = (
                "business_feature_coverage_below_min:"
                f"{len(available_scores)}/{len(dimensions)}",
                *tuple(sorted(missing)),
            )
            continue

        business_score = round(
            sum(item[1] for item in available_scores) / len(available_scores),
            12,
        )
        strongest = sorted(
            available_scores,
            key=lambda item: (-item[1], item[0]),
        )
        weakest = sorted(
            available_scores,
            key=lambda item: (item[1], item[0]),
        )
        supporting = [item[2] for item in strongest if item[1] >= 0.75]
        if not supporting:
            supporting = [f"closest_available:{strongest[0][2]}"]
        counterevidence = [item[2] for item in weakest if item[1] < 0.75]
        counterevidence.extend(sorted(missing))
        if not counterevidence:
            counterevidence = (
                ["counterevidence:none_material_in_available_business_dimensions"]
            )
        provenance = [
            _source_evidence(dimension, target, candidate)
            for dimension, _score, _detail in sorted(available_scores)
        ]
        evidence = (
            *supporting,
            f"business_snapshot_sha256:{feature_snapshot_sha256}",
            *provenance,
        )
        scored.append(
            {
                "symbol": symbol,
                "business_score": business_score,
                "combined_score": business_score,
                "coverage": round(coverage, 12),
                "evidence": tuple(evidence),
                "counterevidence": tuple(counterevidence),
            }
        )

    if not scored:
        raise BusinessSimilarityError(
            "no peer meets the minimum business feature coverage"
        )
    ordered = sorted(
        scored,
        key=lambda item: (
            -float(item["combined_score"]),
            -float(item["coverage"]),
            str(item["symbol"]),
        ),
    )
    selected = ordered[:top_n]
    for item in ordered[top_n:]:
        excluded[str(item["symbol"])] = (f"business_rank_below_top_n:{top_n}",)

    candidates = tuple(
        StockCandidate(
            rank=rank,
            factor_score=None,
            price_volume_score=None,
            **item,
        )
        for rank, item in enumerate(selected, start=1)
    )
    notes = (
        f"business_snapshot_sha256:{feature_snapshot_sha256}",
        f"business_dimensions:{','.join(dimensions)}",
        f"business_min_coverage:{min_coverage:.6f}",
        "business_missing_values:weight_renormalized_never_zero_filled",
        "ranking_tiebreakers:combined_score_desc,coverage_desc,symbol_asc",
    )
    return SimilarityRun(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        factor_evidence_refs=(),
        weights=ChannelWeights(business=1.0, factor=0.0, price_volume=0.0),
        candidates=candidates,
        excluded_symbols=dict(sorted(excluded.items())),
        sensitivity_notes=notes,
    )


def build_business_similarity_object(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: BusinessFeatureSnapshot,
    *,
    top_n: int,
    min_coverage: float,
    created_at: datetime | None = None,
) -> ResearchObject:
    """Create a persisted similarity object descending from its exact peer set."""

    payload = build_business_similarity(
        research,
        data_snapshot,
        peer_set,
        features,
        top_n=top_n,
        min_coverage=min_coverage,
    )
    return create_research_object(
        payload,
        owner_scope=research.owner_scope,
        parent_refs=(research.ref(), data_snapshot.ref(), peer_set.ref()),
        created_at=created_at,
    )
