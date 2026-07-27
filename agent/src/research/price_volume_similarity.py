"""Content-bound qfq price/volume features and deterministic QE3 similarity."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path
from statistics import median, pstdev
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

PRICE_VOLUME_FEATURE_SNAPSHOT_SCHEMA = "vibe.price-volume-feature-snapshot.v1"
PRICE_VOLUME_METRICS = (
    "price_path",
    "return_correlation",
    "volatility",
    "drawdown",
    "volume_path",
    "turnover_path",
    "price_volume_correlation",
)
_MIN_OBSERVATIONS = 3
_METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PriceVolumeSimilarityError(ValueError):
    """Raised when price/volume inputs are incomplete, unbound, or ambiguous."""


class _StrictPriceVolumeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceVolumeObservation(_StrictPriceVolumeModel):
    """One canonical qfq daily observation from a fixed QE2 snapshot."""

    trade_date: date
    close: float = Field(gt=0.0, allow_inf_nan=False)
    volume: float = Field(ge=0.0, allow_inf_nan=False)
    amount: float = Field(ge=0.0, allow_inf_nan=False)


class PriceVolumeFeatureRecord(_StrictPriceVolumeModel):
    """Order-normalized qfq path and explicit suspension facts for one symbol."""

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source_version: str = Field(min_length=1, max_length=128)
    source_fields: tuple[str, ...] = ("amount", "close", "volume")
    observations: tuple[PriceVolumeObservation, ...] = Field(
        min_length=_MIN_OBSERVATIONS
    )
    suspension_dates: tuple[date, ...] = ()

    @field_validator("source_fields")
    @classmethod
    def normalize_source_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result = tuple(sorted(values))
        if len(result) != len(set(result)) or set(result) != {
            "close",
            "volume",
            "amount",
        }:
            raise ValueError("source_fields must be exactly close, volume, and amount")
        return result

    @field_validator("observations")
    @classmethod
    def normalize_observations(
        cls,
        values: tuple[PriceVolumeObservation, ...],
    ) -> tuple[PriceVolumeObservation, ...]:
        result = tuple(sorted(values, key=lambda item: item.trade_date))
        dates = [item.trade_date for item in result]
        if len(dates) != len(set(dates)):
            raise ValueError("price-volume observations must have unique trade dates")
        return result

    @field_validator("suspension_dates")
    @classmethod
    def normalize_suspensions(cls, values: tuple[date, ...]) -> tuple[date, ...]:
        result = tuple(sorted(values))
        if len(result) != len(set(result)):
            raise ValueError("suspension_dates must not contain duplicates")
        return result

    @model_validator(mode="after")
    def validate_suspensions(self) -> "PriceVolumeFeatureRecord":
        observed = {item.trade_date for item in self.observations}
        overlap = observed & set(self.suspension_dates)
        if overlap:
            raise ValueError("suspension dates must not contain a market bar")
        return self


class PriceVolumeFeatureSnapshot(_StrictPriceVolumeModel):
    """Feature input bound to one exact qfq QE2 snapshot."""

    schema_version: Literal["vibe.price-volume-feature-snapshot.v1"] = (
        PRICE_VOLUME_FEATURE_SNAPSHOT_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    data_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: date
    adjustment: Literal["qfq"] = "qfq"
    window_days: int = Field(ge=_MIN_OBSERVATIONS, le=10_000)
    records: tuple[PriceVolumeFeatureRecord, ...] = Field(min_length=2)

    @field_validator("records")
    @classmethod
    def normalize_records(
        cls,
        values: tuple[PriceVolumeFeatureRecord, ...],
    ) -> tuple[PriceVolumeFeatureRecord, ...]:
        result = tuple(sorted(values, key=lambda item: item.symbol))
        symbols = [item.symbol for item in result]
        if len(symbols) != len(set(symbols)):
            raise ValueError("price-volume feature symbols must be unique")
        return result

    @model_validator(mode="after")
    def validate_point_in_time(self) -> "PriceVolumeFeatureSnapshot":
        for record in self.records:
            if len(record.observations) > self.window_days:
                raise ValueError("record exceeds price-volume window_days")
            if any(item.trade_date > self.as_of for item in record.observations):
                raise ValueError("future price-volume observation exceeds as_of")
            if any(item > self.as_of for item in record.suspension_dates):
                raise ValueError("future suspension date exceeds as_of")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    return any(part.is_symlink() for part in (candidate, *candidate.parents))


def load_price_volume_feature_snapshot(path: Path) -> PriceVolumeFeatureSnapshot:
    """Load one strict local feature snapshot without network access."""

    snapshot_path = Path(path)
    if _has_symlink_component(snapshot_path) or not snapshot_path.is_file():
        raise PriceVolumeSimilarityError(
            "price-volume feature snapshot must be a regular, non-symlink file"
        )
    try:
        return PriceVolumeFeatureSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise PriceVolumeSimilarityError(
            f"invalid price-volume feature snapshot: {exc}"
        ) from exc


def _as_trade_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        result = value.date()
        if isinstance(result, date):
            return result
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise PriceVolumeSimilarityError(f"invalid trade date: {value}") from exc


def _finite_number(value: Any, field: str, *, positive: bool) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PriceVolumeSimilarityError(f"invalid {field} value") from exc
    if not math.isfinite(result) or result < 0.0 or (positive and result <= 0.0):
        qualifier = "positive" if positive else "non-negative"
        raise PriceVolumeSimilarityError(f"{field} must be finite and {qualifier}")
    return result


def build_price_volume_feature_snapshot_from_envelope(
    data_snapshot: DataSnapshotRef,
    envelope: Any,
    *,
    window_days: int,
    snapshot_id: str,
) -> PriceVolumeFeatureSnapshot:
    """Materialize qfq paths from an already validated QE2 ``DataEnvelope``.

    The adapter performs no provider access. It verifies the public
    ``DataSnapshotRef`` against the envelope manifest and preserves explicit
    suspension observations instead of forward-filling missing bars.
    """

    if data_snapshot.adjustment != "qfq":
        raise PriceVolumeSimilarityError("price-volume features require qfq data")
    if not _MIN_OBSERVATIONS <= window_days <= 10_000:
        raise PriceVolumeSimilarityError("window_days is outside the supported range")
    required_fields = {"close", "volume", "amount"}
    if not required_fields <= set(data_snapshot.fields):
        raise PriceVolumeSimilarityError(
            "data snapshot must contain close, volume, and amount"
        )
    request = getattr(envelope, "request", None)
    manifest = getattr(envelope, "manifest", None)
    frames = getattr(envelope, "frames", None)
    if request is None or manifest is None or not isinstance(frames, Mapping):
        raise PriceVolumeSimilarityError("invalid QE2 data envelope")
    if (
        getattr(manifest, "snapshot_sha256", None) != data_snapshot.snapshot_sha256
        or getattr(request, "adjustment", None) != "qfq"
        or getattr(request, "end_date", None) != data_snapshot.end_date
        or tuple(getattr(request, "symbols", ())) != data_snapshot.symbols
        or dict(getattr(manifest, "actual_sources", {}))
        != data_snapshot.actual_sources
    ):
        raise PriceVolumeSimilarityError(
            "data envelope identity does not match DataSnapshotRef"
        )
    if set(frames) != set(data_snapshot.symbols):
        raise PriceVolumeSimilarityError(
            "data envelope frames must cover every snapshot symbol"
        )
    source_versions = dict(getattr(manifest, "source_versions", {}))
    suspension_map: dict[str, list[date]] = {
        symbol: [] for symbol in data_snapshot.symbols
    }
    for item in getattr(manifest, "availability", ()):
        if getattr(item, "classification", None) == "suspension":
            suspension_map[getattr(item, "symbol")].append(
                getattr(item, "trade_date")
            )

    records: list[PriceVolumeFeatureRecord] = []
    for symbol in data_snapshot.symbols:
        frame = frames[symbol]
        if not hasattr(frame, "iterrows") or not hasattr(frame, "columns"):
            raise PriceVolumeSimilarityError(f"frame for {symbol} is not tabular")
        if not required_fields <= set(frame.columns):
            raise PriceVolumeSimilarityError(
                f"frame for {symbol} lacks close, volume, or amount"
            )
        rows: list[PriceVolumeObservation] = []
        for index, row in frame.sort_index().iterrows():
            trade_date = _as_trade_date(index)
            if trade_date > data_snapshot.as_of:
                raise PriceVolumeSimilarityError(
                    f"future bar for {symbol} exceeds snapshot as_of"
                )
            rows.append(
                PriceVolumeObservation(
                    trade_date=trade_date,
                    close=_finite_number(row["close"], "close", positive=True),
                    volume=_finite_number(row["volume"], "volume", positive=False),
                    amount=_finite_number(row["amount"], "amount", positive=False),
                )
            )
        rows = rows[-window_days:]
        if len(rows) < _MIN_OBSERVATIONS:
            raise PriceVolumeSimilarityError(
                f"frame for {symbol} has fewer than {_MIN_OBSERVATIONS} observations"
            )
        source = data_snapshot.actual_sources[symbol]
        version = source_versions.get(source)
        if not version:
            raise PriceVolumeSimilarityError(
                f"source version for {source} is missing"
            )
        first_date = rows[0].trade_date
        suspensions = tuple(
            item
            for item in sorted(suspension_map[symbol])
            if first_date <= item <= data_snapshot.as_of
        )
        records.append(
            PriceVolumeFeatureRecord(
                symbol=symbol,
                source=source,
                source_version=version,
                observations=tuple(rows),
                suspension_dates=suspensions,
            )
        )
    return PriceVolumeFeatureSnapshot(
        snapshot_id=snapshot_id,
        data_snapshot_sha256=data_snapshot.snapshot_sha256,
        as_of=data_snapshot.as_of,
        window_days=window_days,
        records=tuple(records),
    )


def _require_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: PriceVolumeFeatureSnapshot,
) -> tuple[ResearchSpec, DataSnapshotRef, PeerSet]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload,
        ResearchSpec,
    ):
        raise PriceVolumeSimilarityError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload,
        DataSnapshotRef,
    ):
        raise PriceVolumeSimilarityError(
            "data_snapshot must be a data_snapshot_ref object"
        )
    if peer_set.object_type != "peer_set" or not isinstance(
        peer_set.payload,
        PeerSet,
    ):
        raise PriceVolumeSimilarityError("peer_set must be a peer_set object")
    if len({research.owner_scope, data_snapshot.owner_scope, peer_set.owner_scope}) != 1:
        raise PriceVolumeSimilarityError(
            "price-volume inputs cross owner_scope boundaries"
        )
    if research.ref() not in data_snapshot.parent_refs:
        raise PriceVolumeSimilarityError(
            "data_snapshot does not descend from the research_spec"
        )
    if (
        research.ref() not in peer_set.parent_refs
        or data_snapshot.ref() not in peer_set.parent_refs
    ):
        raise PriceVolumeSimilarityError(
            "peer_set does not descend from research and data snapshot"
        )
    spec = research.payload
    snapshot_ref = data_snapshot.payload
    peers = peer_set.payload
    if (
        peers.research_spec_ref != research.ref()
        or peers.data_snapshot_ref != data_snapshot.ref()
    ):
        raise PriceVolumeSimilarityError("peer_set payload references do not match inputs")
    if snapshot_ref.adjustment != "qfq" or features.adjustment != "qfq":
        raise PriceVolumeSimilarityError("price-volume similarity requires qfq inputs")
    if features.data_snapshot_sha256 != snapshot_ref.snapshot_sha256:
        raise PriceVolumeSimilarityError(
            "price-volume features do not bind the selected data snapshot"
        )
    if features.as_of != spec.as_of or snapshot_ref.as_of != spec.as_of:
        raise PriceVolumeSimilarityError(
            "price-volume features, research, and data snapshot must share as_of"
        )
    required_symbols = {peers.target_symbol, *peers.members}
    if required_symbols - set(snapshot_ref.symbols):
        raise PriceVolumeSimilarityError(
            "data snapshot does not cover target and all peer members"
        )
    return spec, snapshot_ref, peers


def _normalize_weights(metric_weights: Mapping[str, float]) -> tuple[tuple[str, float], ...]:
    if not metric_weights:
        raise PriceVolumeSimilarityError("metric_weights must not be empty")
    unknown = set(metric_weights) - set(PRICE_VOLUME_METRICS)
    if unknown:
        raise PriceVolumeSimilarityError(f"unsupported price-volume metrics: {sorted(unknown)}")
    ordered: list[tuple[str, float]] = []
    for metric_id, weight in sorted(metric_weights.items()):
        if _METRIC_ID_RE.fullmatch(metric_id) is None:
            raise PriceVolumeSimilarityError(f"invalid metric_id: {metric_id}")
        if not math.isfinite(weight) or weight <= 0.0:
            raise PriceVolumeSimilarityError(
                "price-volume weights must be finite and positive"
            )
        ordered.append((metric_id, float(weight)))
    total = sum(weight for _metric_id, weight in ordered)
    return tuple((metric_id, weight / total) for metric_id, weight in ordered)


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [item - left_mean for item in left]
    right_centered = [item - right_mean for item in right]
    left_norm = math.sqrt(sum(item * item for item in left_centered))
    right_norm = math.sqrt(sum(item * item for item in right_centered))
    if left_norm <= 1e-15 or right_norm <= 1e-15:
        return None
    result = sum(a * b for a, b in zip(left_centered, right_centered)) / (
        left_norm * right_norm
    )
    return max(-1.0, min(1.0, result))


def _rmse(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)) / len(left))


def _normalized_log_path(values: Sequence[float]) -> list[float] | None:
    if not values or values[0] <= 0.0:
        return None
    return [math.log(item / values[0]) for item in values]


def _normalized_activity_path(values: Sequence[float]) -> list[float] | None:
    positive = [item for item in values if item > 0.0]
    if not positive:
        return None
    scale = median(positive)
    return [math.log1p(item / scale) for item in values]


def _returns(values: Sequence[float]) -> list[float]:
    return [math.log(current / previous) for previous, current in zip(values, values[1:])]


def _max_drawdown(values: Sequence[float]) -> float:
    peak = values[0]
    result = 0.0
    for value in values:
        peak = max(peak, value)
        result = max(result, 1.0 - value / peak)
    return result


def _ratio_similarity(left: float, right: float) -> float:
    if left == 0.0 and right == 0.0:
        return 1.0
    if left <= 0.0 or right <= 0.0:
        return 0.0
    return 1.0 / (1.0 + abs(math.log(left / right)))


def _record_map(record: PriceVolumeFeatureRecord) -> dict[date, PriceVolumeObservation]:
    return {item.trade_date: item for item in record.observations}


def _metric_values(
    target: PriceVolumeFeatureRecord,
    candidate: PriceVolumeFeatureRecord,
) -> tuple[dict[str, float], float, tuple[str, ...]]:
    target_map = _record_map(target)
    candidate_map = _record_map(candidate)
    common_dates = sorted(set(target_map) & set(candidate_map))
    total_dates = len(set(target_map) | set(candidate_map))
    if len(common_dates) < _MIN_OBSERVATIONS or total_dates == 0:
        return {}, 0.0, ("common_dates_below_min",)
    target_rows = [target_map[item] for item in common_dates]
    candidate_rows = [candidate_map[item] for item in common_dates]
    target_close = [item.close for item in target_rows]
    candidate_close = [item.close for item in candidate_rows]
    target_returns = _returns(target_close)
    candidate_returns = _returns(candidate_close)
    metrics: dict[str, float] = {}

    target_price_path = _normalized_log_path(target_close)
    candidate_price_path = _normalized_log_path(candidate_close)
    if target_price_path is not None and candidate_price_path is not None:
        metrics["price_path"] = 1.0 / (
            1.0 + _rmse(target_price_path, candidate_price_path)
        )
    return_corr = _correlation(target_returns, candidate_returns)
    if return_corr is not None:
        metrics["return_correlation"] = (return_corr + 1.0) / 2.0
    metrics["volatility"] = _ratio_similarity(
        pstdev(target_returns),
        pstdev(candidate_returns),
    )
    metrics["drawdown"] = max(
        0.0,
        1.0 - abs(_max_drawdown(target_close) - _max_drawdown(candidate_close)),
    )

    for metric_id, field in (("volume_path", "volume"), ("turnover_path", "amount")):
        target_values = [getattr(item, field) for item in target_rows]
        candidate_values = [getattr(item, field) for item in candidate_rows]
        target_path = _normalized_activity_path(target_values)
        candidate_path = _normalized_activity_path(candidate_values)
        if target_path is not None and candidate_path is not None:
            metrics[metric_id] = 1.0 / (1.0 + _rmse(target_path, candidate_path))

    target_volume = [item.volume for item in target_rows]
    candidate_volume = [item.volume for item in candidate_rows]
    if all(item > 0.0 for item in (*target_volume, *candidate_volume)):
        target_volume_returns = [
            math.log(current / previous)
            for previous, current in zip(target_volume, target_volume[1:])
        ]
        candidate_volume_returns = [
            math.log(current / previous)
            for previous, current in zip(candidate_volume, candidate_volume[1:])
        ]
        target_pv_corr = _correlation(target_returns, target_volume_returns)
        candidate_pv_corr = _correlation(candidate_returns, candidate_volume_returns)
        if target_pv_corr is not None and candidate_pv_corr is not None:
            metrics["price_volume_correlation"] = max(
                0.0,
                1.0 - abs(target_pv_corr - candidate_pv_corr) / 2.0,
            )

    target_suspensions = set(target.suspension_dates)
    candidate_suspensions = set(candidate.suspension_dates)
    suspension_difference = sorted(target_suspensions ^ candidate_suspensions)
    notes = tuple(
        [f"common_dates:{len(common_dates)}/{total_dates}"]
        + (
            [f"suspension_mismatch:{len(suspension_difference)}"]
            if suspension_difference
            else []
        )
    )
    return metrics, len(common_dates) / total_dates, notes


def build_price_volume_similarity(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: PriceVolumeFeatureSnapshot,
    *,
    metric_weights: Mapping[str, float],
    top_n: int,
    min_coverage: float,
) -> SimilarityRun:
    """Rank peers by qfq price/volume paths without filling absent dates."""

    _spec, _snapshot_ref, peers = _require_inputs(
        research,
        data_snapshot,
        peer_set,
        features,
    )
    if not 1 <= top_n <= 500:
        raise PriceVolumeSimilarityError("top_n must be between 1 and 500")
    if not 0.0 < min_coverage <= 1.0:
        raise PriceVolumeSimilarityError("min_coverage must be in (0, 1]")
    weights = _normalize_weights(metric_weights)
    records = {item.symbol: item for item in features.records}
    target = records.get(peers.target_symbol)
    if target is None:
        raise PriceVolumeSimilarityError("target price-volume record is missing")
    total_weight = sum(weight for _metric_id, weight in weights)
    excluded = {
        symbol: tuple(reasons)
        for symbol, reasons in sorted(peers.excluded_reasons.items())
    }
    scored: list[dict[str, object]] = []
    for symbol in sorted(peers.members):
        candidate = records.get(symbol)
        if candidate is None:
            excluded[symbol] = ("price_volume_record_missing",)
            continue
        metrics, date_coverage, metric_notes = _metric_values(target, candidate)
        available = [
            (metric_id, weight, metrics[metric_id])
            for metric_id, weight in weights
            if metric_id in metrics
        ]
        missing = [
            f"missing_price_volume_metric:{metric_id}"
            for metric_id, _weight in weights
            if metric_id not in metrics
        ]
        available_weight = sum(item[1] for item in available)
        coverage = (available_weight / total_weight) * date_coverage
        coverage = round(coverage, 12)
        if coverage < min_coverage or not available:
            excluded[symbol] = (
                f"price_volume_coverage_below_min:{coverage:.6f}",
                *tuple(sorted(missing)),
                *metric_notes,
            )
            continue
        score = round(
            (
                sum(weight * value for _metric_id, weight, value in available)
                / available_weight
            )
            * date_coverage,
            12,
        )
        strongest = sorted(available, key=lambda item: (-item[2], item[0]))
        weakest = sorted(available, key=lambda item: (item[2], item[0]))
        evidence = [
            f"price_volume_match:{metric_id}:similarity={value:.6f}"
            for metric_id, _weight, value in strongest[:3]
        ]
        evidence.extend(
            (
                f"price_volume_snapshot_sha256:{features.snapshot_sha256}",
                f"data_snapshot_sha256:{features.data_snapshot_sha256}",
                f"price_volume_source:{candidate.source}@{candidate.source_version}",
            )
        )
        counterevidence = [
            f"price_volume_gap:{metric_id}:similarity={value:.6f}"
            for metric_id, _weight, value in weakest
            if value < 0.5
        ]
        counterevidence.extend(missing)
        counterevidence.extend(
            item for item in metric_notes if item.startswith("suspension_mismatch:")
        )
        if not counterevidence:
            counterevidence = ["counterevidence:none_material_in_available_metrics"]
        scored.append(
            {
                "symbol": symbol,
                "price_volume_score": score,
                "combined_score": score,
                "coverage": coverage,
                "evidence": tuple(evidence),
                "counterevidence": tuple(counterevidence),
            }
        )
    if not scored:
        raise PriceVolumeSimilarityError(
            "no peer meets the minimum price-volume coverage"
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
        excluded[str(item["symbol"])] = (
            f"price_volume_rank_below_top_n:{top_n}",
        )
    candidates = tuple(
        StockCandidate(
            rank=rank,
            business_score=None,
            factor_score=None,
            **item,
        )
        for rank, item in enumerate(selected, start=1)
    )
    normalized_weights = ",".join(
        f"{metric_id}={weight:.12f}" for metric_id, weight in weights
    )
    notes = (
        f"price_volume_snapshot_sha256:{features.snapshot_sha256}",
        f"data_snapshot_sha256:{features.data_snapshot_sha256}",
        f"price_volume_metric_weights:{normalized_weights}",
        f"price_volume_min_coverage:{min_coverage:.6f}",
        "price_adjustment:qfq_required",
        "missing_dates:not_forward_filled",
        "activity_scaling:median_positive_log1p",
        "ranking_tiebreakers:combined_score_desc,coverage_desc,symbol_asc",
    )
    return SimilarityRun(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        factor_evidence_refs=(),
        weights=ChannelWeights(business=0.0, factor=0.0, price_volume=1.0),
        candidates=candidates,
        excluded_symbols=dict(sorted(excluded.items())),
        sensitivity_notes=notes,
    )


def build_price_volume_similarity_object(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    features: PriceVolumeFeatureSnapshot,
    *,
    metric_weights: Mapping[str, float],
    top_n: int,
    min_coverage: float,
    created_at: datetime | None = None,
) -> ResearchObject:
    """Create a persisted price-volume result with a closed parent chain."""

    payload = build_price_volume_similarity(
        research,
        data_snapshot,
        peer_set,
        features,
        metric_weights=metric_weights,
        top_n=top_n,
        min_coverage=min_coverage,
    )
    return create_research_object(
        payload,
        owner_scope=research.owner_scope,
        parent_refs=(research.ref(), data_snapshot.ref(), peer_set.ref()),
        created_at=created_at,
    )
