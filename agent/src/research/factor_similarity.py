"""Content-bound factor features and robust QE3 similarity scoring."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Literal
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

FACTOR_FEATURE_SNAPSHOT_SCHEMA = "vibe.factor-feature-snapshot.v1"
ROBUST_Z_CLIP = 12.0
MIN_FACTOR_OBSERVATIONS = 3
_FACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")


class FactorSimilarityError(ValueError):
    """Raised when factor similarity inputs are incomplete or unbound."""


class _StrictFactorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactorFeatureValue(_StrictFactorModel):
    """One finite factor value with exact point-in-time provenance."""

    factor_id: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    value: float = Field(allow_inf_nan=False)
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
        if any(not value or len(value) > 128 for value in result):
            raise ValueError("source_fields contain an invalid identifier")
        return result


class FactorFeatureRecord(_StrictFactorModel):
    """Order-normalized factor vector for one symbol."""

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    values: tuple[FactorFeatureValue, ...] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def normalize_values(
        cls,
        values: tuple[FactorFeatureValue, ...],
    ) -> tuple[FactorFeatureValue, ...]:
        result = tuple(sorted(values, key=lambda item: item.factor_id))
        ids = [item.factor_id for item in result]
        if len(ids) != len(set(ids)):
            raise ValueError("factor values must have unique factor_id values")
        return result

    def value_map(self) -> dict[str, FactorFeatureValue]:
        return {item.factor_id: item for item in self.values}


class FactorFeatureSnapshot(_StrictFactorModel):
    """Factor vectors visible at one exact as-of cutoff."""

    schema_version: Literal["vibe.factor-feature-snapshot.v1"] = (
        FACTOR_FEATURE_SNAPSHOT_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    as_of: date
    cutoff_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    records: tuple[FactorFeatureRecord, ...] = Field(min_length=2)

    @field_validator("records")
    @classmethod
    def normalize_records(
        cls,
        values: tuple[FactorFeatureRecord, ...],
    ) -> tuple[FactorFeatureRecord, ...]:
        return tuple(sorted(values, key=lambda item: item.symbol))

    @model_validator(mode="after")
    def validate_snapshot(self) -> "FactorFeatureSnapshot":
        symbols = [item.symbol for item in self.records]
        if len(symbols) != len(set(symbols)):
            raise ValueError("factor feature symbols must be unique")
        timezone = ZoneInfo(self.cutoff_timezone)
        for record in self.records:
            for value in record.values:
                if value.known_at.astimezone(timezone).date() > self.as_of:
                    raise ValueError(
                        f"future-known factor {value.factor_id} for "
                        f"{record.symbol} exceeds as_of"
                    )
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)


class RobustFactorStats(_StrictFactorModel):
    """Auditable robust location and scale used for one factor."""

    factor_id: str
    observation_count: int = Field(ge=MIN_FACTOR_OBSERVATIONS)
    median: float
    mad: float = Field(ge=0.0)
    scale: float = Field(gt=0.0)
    scale_method: Literal["mad", "minimum_nonzero_deviation", "constant"]

    def robust_z(self, value: float) -> float:
        result = (value - self.median) / self.scale
        return round(max(-ROBUST_Z_CLIP, min(ROBUST_Z_CLIP, result)), 12)


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    return any(part.is_symlink() for part in (candidate, *candidate.parents))


def load_factor_feature_snapshot(path: Path) -> FactorFeatureSnapshot:
    """Load a strict local factor snapshot without network access."""

    snapshot_path = Path(path)
    if _has_symlink_component(snapshot_path) or not snapshot_path.is_file():
        raise FactorSimilarityError(
            "factor feature snapshot must be a regular, non-symlink file"
        )
    try:
        return FactorFeatureSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise FactorSimilarityError(f"invalid factor feature snapshot: {exc}") from exc


def _require_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: FactorFeatureSnapshot,
) -> tuple[ResearchSpec, DataSnapshotRef, PeerSet]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload,
        ResearchSpec,
    ):
        raise FactorSimilarityError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload,
        DataSnapshotRef,
    ):
        raise FactorSimilarityError("data_snapshot must be a data_snapshot_ref object")
    if peer_set.object_type != "peer_set" or not isinstance(
        peer_set.payload,
        PeerSet,
    ):
        raise FactorSimilarityError("peer_set must be a peer_set object")
    if len({research.owner_scope, data_snapshot.owner_scope, peer_set.owner_scope}) != 1:
        raise FactorSimilarityError("factor similarity inputs cross owner_scope boundaries")
    if research.ref() not in data_snapshot.parent_refs:
        raise FactorSimilarityError(
            "data_snapshot does not descend from the research_spec"
        )
    if research.ref() not in peer_set.parent_refs or data_snapshot.ref() not in peer_set.parent_refs:
        raise FactorSimilarityError(
            "peer_set does not descend from research and data snapshot"
        )
    spec = research.payload
    snapshot_ref = data_snapshot.payload
    peers = peer_set.payload
    if peers.research_spec_ref != research.ref() or peers.data_snapshot_ref != data_snapshot.ref():
        raise FactorSimilarityError("peer_set payload references do not match inputs")
    if features.as_of != spec.as_of or snapshot_ref.as_of != spec.as_of:
        raise FactorSimilarityError(
            "factor features, research, and data snapshot must share as_of"
        )
    required_symbols = {peers.target_symbol, *peers.members}
    if required_symbols - set(snapshot_ref.symbols):
        raise FactorSimilarityError(
            "data snapshot does not cover target and all peer members"
        )
    return spec, snapshot_ref, peers


def _normalize_weights(factor_weights: dict[str, float]) -> tuple[tuple[str, float], ...]:
    if not factor_weights:
        raise FactorSimilarityError("factor_weights must not be empty")
    ordered: list[tuple[str, float]] = []
    for factor_id, weight in sorted(factor_weights.items()):
        if _FACTOR_ID_RE.fullmatch(factor_id) is None:
            raise FactorSimilarityError(f"invalid factor_id: {factor_id}")
        if not math.isfinite(weight) or weight <= 0.0:
            raise FactorSimilarityError("factor weights must be finite and positive")
        ordered.append((factor_id, float(weight)))
    total = sum(weight for _factor_id, weight in ordered)
    return tuple(
        (factor_id, round(weight / total, 12))
        for factor_id, weight in ordered
    )


def _robust_stats(factor_id: str, values: list[float]) -> RobustFactorStats:
    if len(values) < MIN_FACTOR_OBSERVATIONS:
        raise FactorSimilarityError(
            f"factor {factor_id} has fewer than {MIN_FACTOR_OBSERVATIONS} observations"
        )
    location = float(median(values))
    deviations = [abs(value - location) for value in values]
    mad = float(median(deviations))
    if mad > 0.0:
        scale = mad / 0.6744897501960817
        method = "mad"
    else:
        nonzero = sorted(value for value in deviations if value > 0.0)
        if nonzero:
            scale = nonzero[0]
            method = "minimum_nonzero_deviation"
        else:
            scale = 1.0
            method = "constant"
    return RobustFactorStats(
        factor_id=factor_id,
        observation_count=len(values),
        median=location,
        mad=mad,
        scale=scale,
        scale_method=method,
    )


def _source_evidence(
    factor_id: str,
    target: FactorFeatureValue,
    candidate: FactorFeatureValue,
) -> str:
    return (
        f"factor_source:{factor_id}:"
        f"target={target.source}@{target.source_version};"
        f"candidate={candidate.source}@{candidate.source_version}"
    )


def build_factor_similarity(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: FactorFeatureSnapshot,
    *,
    factor_weights: dict[str, float],
    top_n: int,
    min_coverage: float,
) -> SimilarityRun:
    """Rank peers by robust-z distance over jointly available factor values."""

    spec, _snapshot_ref, peers = _require_inputs(
        research,
        data_snapshot,
        peer_set,
        features,
    )
    if not 1 <= top_n <= 500:
        raise FactorSimilarityError("top_n must be between 1 and 500")
    if not 0.0 < min_coverage <= 1.0:
        raise FactorSimilarityError("min_coverage must be in (0, 1]")
    weights = _normalize_weights(factor_weights)
    records = {item.symbol: item.value_map() for item in features.records}
    target_values = records.get(peers.target_symbol)
    if target_values is None:
        raise FactorSimilarityError("target factor feature record is missing")

    stats: dict[str, RobustFactorStats] = {}
    unavailable_factors: dict[str, str] = {}
    cross_section = (peers.target_symbol, *tuple(sorted(peers.members)))
    for factor_id, _weight in weights:
        values = [
            records[symbol][factor_id].value
            for symbol in cross_section
            if symbol in records and factor_id in records[symbol]
        ]
        if factor_id not in target_values:
            unavailable_factors[factor_id] = "target_factor_missing"
            continue
        if len(values) < MIN_FACTOR_OBSERVATIONS:
            unavailable_factors[factor_id] = (
                f"factor_observations_below_min:{len(values)}/"
                f"{MIN_FACTOR_OBSERVATIONS}"
            )
            continue
        stats[factor_id] = _robust_stats(factor_id, values)
    if not stats:
        raise FactorSimilarityError("no requested factor has a usable cross-section")

    total_weight = sum(weight for _factor_id, weight in weights)
    excluded = {
        symbol: tuple(reasons)
        for symbol, reasons in sorted(peers.excluded_reasons.items())
    }
    scored: list[dict[str, object]] = []
    for symbol in sorted(peers.members):
        candidate_values = records.get(symbol)
        if candidate_values is None:
            excluded[symbol] = ("factor_feature_record_missing",)
            continue
        available: list[
            tuple[str, float, float, float, FactorFeatureValue, FactorFeatureValue]
        ] = []
        missing: list[str] = []
        for factor_id, weight in weights:
            if factor_id not in stats:
                missing.append(
                    f"unusable_factor:{factor_id}:{unavailable_factors[factor_id]}"
                )
                continue
            candidate_value = candidate_values.get(factor_id)
            target_value = target_values.get(factor_id)
            if candidate_value is None or target_value is None:
                missing.append(f"missing_factor:{factor_id}:candidate")
                continue
            factor_stats = stats[factor_id]
            target_z = factor_stats.robust_z(target_value.value)
            candidate_z = factor_stats.robust_z(candidate_value.value)
            distance = round(abs(candidate_z - target_z), 12)
            similarity = round(1.0 / (1.0 + distance), 12)
            available.append(
                (
                    factor_id,
                    weight,
                    similarity,
                    distance,
                    target_value,
                    candidate_value,
                )
            )

        available_weight = sum(item[1] for item in available)
        coverage = round(available_weight / total_weight, 12)
        if coverage < min_coverage:
            excluded[symbol] = (
                f"factor_coverage_below_min:{coverage:.6f}",
                *tuple(sorted(missing)),
            )
            continue
        factor_score = round(
            sum(item[1] * item[2] for item in available) / available_weight,
            12,
        )
        strongest = sorted(available, key=lambda item: (-item[2], item[0]))
        weakest = sorted(available, key=lambda item: (item[2], item[0]))
        supporting = [
            (
                f"factor_match:{item[0]}:similarity={item[2]:.6f};"
                f"robust_z_distance={item[3]:.6f}"
            )
            for item in strongest
            if item[2] >= 0.5
        ]
        if not supporting:
            item = strongest[0]
            supporting = [
                f"closest_factor:{item[0]}:similarity={item[2]:.6f};"
                f"robust_z_distance={item[3]:.6f}"
            ]
        counterevidence = [
            (
                f"factor_gap:{item[0]}:similarity={item[2]:.6f};"
                f"robust_z_distance={item[3]:.6f}"
            )
            for item in weakest
            if item[2] < 0.5
        ]
        counterevidence.extend(sorted(missing))
        if not counterevidence:
            counterevidence = ["counterevidence:none_material_in_available_factors"]
        provenance = [
            _source_evidence(item[0], item[4], item[5])
            for item in sorted(available)
        ]
        evidence = (
            *supporting,
            f"factor_snapshot_sha256:{features.snapshot_sha256}",
            *provenance,
        )
        scored.append(
            {
                "symbol": symbol,
                "factor_score": factor_score,
                "combined_score": factor_score,
                "coverage": coverage,
                "evidence": tuple(evidence),
                "counterevidence": tuple(counterevidence),
            }
        )
    if not scored:
        raise FactorSimilarityError("no peer meets the minimum factor coverage")

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
        excluded[str(item["symbol"])] = (f"factor_rank_below_top_n:{top_n}",)
    candidates = tuple(
        StockCandidate(
            rank=rank,
            business_score=None,
            price_volume_score=None,
            **item,
        )
        for rank, item in enumerate(selected, start=1)
    )
    stats_notes = tuple(
        (
            f"robust_stats:{factor_id}:n={item.observation_count};"
            f"median={item.median:.12g};mad={item.mad:.12g};"
            f"scale={item.scale:.12g};method={item.scale_method}"
        )
        for factor_id, item in sorted(stats.items())
    )
    normalized_weights = ",".join(
        f"{factor_id}={weight:.12f}" for factor_id, weight in weights
    )
    notes = (
        f"factor_snapshot_sha256:{features.snapshot_sha256}",
        f"factor_weights:{normalized_weights}",
        f"factor_min_coverage:{min_coverage:.6f}",
        f"robust_z_clip:{ROBUST_Z_CLIP:.6f}",
        "factor_missing_values:weight_renormalized_never_zero_filled",
        "ranking_tiebreakers:combined_score_desc,coverage_desc,symbol_asc",
        *stats_notes,
    )
    return SimilarityRun(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        factor_evidence_refs=(),
        weights=ChannelWeights(business=0.0, factor=1.0, price_volume=0.0),
        candidates=candidates,
        excluded_symbols=dict(sorted(excluded.items())),
        sensitivity_notes=notes,
    )


def build_factor_similarity_object(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: FactorFeatureSnapshot,
    *,
    factor_weights: dict[str, float],
    top_n: int,
    min_coverage: float,
    created_at: datetime | None = None,
) -> ResearchObject:
    """Create a persisted factor similarity object with a closed parent chain."""

    payload = build_factor_similarity(
        research,
        data_snapshot,
        peer_set,
        features,
        factor_weights=factor_weights,
        top_n=top_n,
        min_coverage=min_coverage,
    )
    return create_research_object(
        payload,
        owner_scope=research.owner_scope,
        parent_refs=(research.ref(), data_snapshot.ref(), peer_set.ref()),
        created_at=created_at,
    )
