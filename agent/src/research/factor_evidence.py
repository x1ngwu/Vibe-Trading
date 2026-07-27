"""Point-in-time common factor anomalies relative to each target's own peers."""

from __future__ import annotations

import math
import re
from datetime import datetime
from statistics import median
from typing import Sequence

from .contracts import (
    DataSnapshotRef,
    FactorEvidence,
    FactorObservation,
    PeerSet,
    ResearchObject,
    ResearchSpec,
    create_research_object,
)
from .factor_similarity import (
    MIN_FACTOR_OBSERVATIONS,
    ROBUST_Z_CLIP,
    FactorFeatureSnapshot,
    FactorFeatureValue,
)

_FACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")


class FactorEvidenceError(ValueError):
    """Raised when peer-relative common anomalies cannot be reconstructed."""


def _require_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_sets: Sequence[ResearchObject],
    features: FactorFeatureSnapshot,
) -> tuple[ResearchSpec, DataSnapshotRef, tuple[ResearchObject, ...]]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload,
        ResearchSpec,
    ):
        raise FactorEvidenceError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload,
        DataSnapshotRef,
    ):
        raise FactorEvidenceError("data_snapshot must be a data_snapshot_ref object")
    if research.ref() not in data_snapshot.parent_refs:
        raise FactorEvidenceError(
            "data_snapshot does not descend from the research_spec"
        )
    normalized = tuple(
        sorted(peer_sets, key=lambda item: getattr(item.payload, "target_symbol", ""))
    )
    if len(normalized) < 2:
        raise FactorEvidenceError("common anomalies require at least two peer sets")
    targets: list[str] = []
    required_symbols: set[str] = set()
    for item in normalized:
        if item.object_type != "peer_set" or not isinstance(item.payload, PeerSet):
            raise FactorEvidenceError("peer_sets may only contain peer_set objects")
        if item.owner_scope != research.owner_scope:
            raise FactorEvidenceError("factor evidence inputs cross owner_scope")
        if (
            research.ref() not in item.parent_refs
            or data_snapshot.ref() not in item.parent_refs
        ):
            raise FactorEvidenceError(
                "peer_set does not descend from research and data snapshot"
            )
        peers = item.payload
        if (
            peers.research_spec_ref != research.ref()
            or peers.data_snapshot_ref != data_snapshot.ref()
        ):
            raise FactorEvidenceError("peer_set payload references do not match")
        targets.append(peers.target_symbol)
        required_symbols.update((peers.target_symbol, *peers.members))
    if len(targets) != len(set(targets)):
        raise FactorEvidenceError("peer sets must have unique target symbols")
    if set(targets) != set(research.payload.symbols):
        raise FactorEvidenceError(
            "research symbols must exactly match the peer-set targets"
        )
    if required_symbols - set(data_snapshot.payload.symbols):
        raise FactorEvidenceError("data snapshot does not cover every target and peer")
    if (
        features.as_of != research.payload.as_of
        or data_snapshot.payload.as_of != research.payload.as_of
    ):
        raise FactorEvidenceError(
            "factor features, research, and data snapshot must share as_of"
        )
    return research.payload, data_snapshot.payload, normalized


def _normalize_factor_ids(factor_ids: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted(factor_ids))
    if not result:
        raise FactorEvidenceError("factor_ids must not be empty")
    if len(result) != len(set(result)):
        raise FactorEvidenceError("factor_ids must not contain duplicates")
    if any(_FACTOR_ID_RE.fullmatch(item) is None for item in result):
        raise FactorEvidenceError("factor_ids contain an invalid identifier")
    return result


def _robust_location_scale(values: Sequence[float]) -> tuple[float, float, str]:
    if len(values) < MIN_FACTOR_OBSERVATIONS:
        raise FactorEvidenceError("peer factor cross-section is below minimum")
    location = float(median(values))
    deviations = [abs(value - location) for value in values]
    mad = float(median(deviations))
    if mad > 0.0:
        return location, mad / 0.6744897501960817, "mad"
    nonzero = sorted(value for value in deviations if value > 0.0)
    if nonzero:
        return location, nonzero[0], "minimum_nonzero_deviation"
    return location, 1.0, "constant"


def _robust_z(value: float, location: float, scale: float) -> float:
    result = (value - location) / scale
    return round(max(-ROBUST_Z_CLIP, min(ROBUST_Z_CLIP, result)), 12)


def _percentile(value: float, peers: Sequence[float]) -> float:
    lower = sum(item < value for item in peers)
    equal = sum(item == value for item in peers)
    return round((lower + 0.5 * equal) / len(peers), 12)


def _source_note(
    factor_id: str,
    symbol: str,
    value: FactorFeatureValue,
) -> str:
    return (
        f"factor_source:{factor_id}:{symbol}="
        f"{value.source}@{value.source_version};"
        f"known_at={value.known_at.isoformat()};"
        f"fields={','.join(value.source_fields)}"
    )


def build_common_factor_evidence(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_sets: Sequence[ResearchObject],
    features: FactorFeatureSnapshot,
    *,
    factor_ids: Sequence[str],
    min_abs_z: float,
    min_coverage: float,
    min_direction_agreement: float,
    min_peer_observations: int = MIN_FACTOR_OBSERVATIONS,
) -> tuple[FactorEvidence, ...]:
    """Compare every target with its own peers before finding shared anomalies."""

    if not math.isfinite(min_abs_z) or min_abs_z <= 0.0:
        raise FactorEvidenceError("min_abs_z must be finite and positive")
    if not 0.0 < min_coverage <= 1.0:
        raise FactorEvidenceError("min_coverage must be in (0, 1]")
    if not 0.5 < min_direction_agreement <= 1.0:
        raise FactorEvidenceError("min_direction_agreement must be in (0.5, 1]")
    if min_peer_observations < MIN_FACTOR_OBSERVATIONS:
        raise FactorEvidenceError(
            f"min_peer_observations must be at least {MIN_FACTOR_OBSERVATIONS}"
        )
    _spec, _snapshot, normalized_peer_sets = _require_inputs(
        research,
        data_snapshot,
        peer_sets,
        features,
    )
    normalized_factor_ids = _normalize_factor_ids(factor_ids)
    records = {item.symbol: item.value_map() for item in features.records}
    results: list[FactorEvidence] = []
    unusable: list[str] = []
    for factor_id in normalized_factor_ids:
        observations: list[FactorObservation] = []
        source_notes: list[str] = []
        limitations: list[str] = []
        for peer_object in normalized_peer_sets:
            peers = peer_object.payload
            target_values = records.get(peers.target_symbol, {})
            target = target_values.get(factor_id)
            if target is None:
                limitations.append(
                    f"target_factor_missing:{peers.target_symbol}:{factor_id}"
                )
                continue
            peer_features = [
                records[symbol][factor_id]
                for symbol in sorted(peers.members)
                if symbol in records and factor_id in records[symbol]
            ]
            peer_values = [item.value for item in peer_features]
            if len(peer_values) < min_peer_observations:
                limitations.append(
                    f"peer_factor_observations_below_min:{peers.target_symbol}:"
                    f"{len(peer_values)}/{min_peer_observations}"
                )
                continue
            location, scale, method = _robust_location_scale(peer_values)
            observations.append(
                FactorObservation(
                    symbol=peers.target_symbol,
                    value=target.value,
                    peer_median=location,
                    robust_zscore=_robust_z(target.value, location, scale),
                    percentile=_percentile(target.value, peer_values),
                    source_fields=target.source_fields,
                )
            )
            source_notes.append(_source_note(factor_id, peers.target_symbol, target))
            peer_sources = sorted(
                {
                    f"{item.source}@{item.source_version}:"
                    f"known_at={item.known_at.isoformat()}:"
                    f"fields={','.join(item.source_fields)}"
                    for item in peer_features
                }
            )
            limitations.extend(
                (
                    f"peer_baseline:{peers.target_symbol}:n={len(peer_values)};"
                    f"median={location:.12g};scale={scale:.12g};method={method}",
                    f"peer_factor_sources:{factor_id}:{peers.target_symbol}:"
                    f"{';'.join(peer_sources)}",
                )
            )
        observations.sort(key=lambda item: item.symbol)
        coverage = len(observations) / len(normalized_peer_sets)
        if coverage < min_coverage:
            unusable.append(
                f"{factor_id}:coverage={coverage:.6f}<min={min_coverage:.6f}"
            )
            continue
        positive = tuple(
            item.symbol for item in observations if item.robust_zscore >= min_abs_z
        )
        negative = tuple(
            item.symbol for item in observations if item.robust_zscore <= -min_abs_z
        )
        dominant_count = max(len(positive), len(negative))
        direction_agreement = dominant_count / len(observations)
        has_common_support = (
            dominant_count >= 2
            and direction_agreement >= min_direction_agreement
            and len(positive) != len(negative)
        )
        if has_common_support and len(positive) > len(negative):
            direction = "positive"
            supporting = positive
        elif has_common_support:
            direction = "negative"
            supporting = negative
        else:
            direction = "mixed"
            supporting = ()
            limitations.append(
                "common_direction_below_threshold:"
                f"agreement={direction_agreement:.6f};"
                f"min={min_direction_agreement:.6f}"
            )
        contradicting = tuple(
            item.symbol for item in observations if item.symbol not in supporting
        )
        stability = round(
            (len(supporting) / len(observations)) if supporting else 0.0,
            12,
        )
        limitations.extend(
            (
                f"factor_snapshot_sha256:{features.snapshot_sha256}",
                f"common_anomaly_min_abs_z:{min_abs_z:.6f}",
                f"common_anomaly_min_direction_agreement:"
                f"{min_direction_agreement:.6f}",
                "stability_scope:single_window_direction_consistency",
                *sorted(source_notes),
            )
        )
        results.append(
            FactorEvidence(
                research_spec_ref=research.ref(),
                data_snapshot_ref=data_snapshot.ref(),
                peer_set_refs=tuple(item.ref() for item in normalized_peer_sets),
                factor_id=factor_id,
                direction=direction,
                observations=tuple(observations),
                supporting_symbols=supporting,
                contradicting_symbols=contradicting,
                coverage=round(coverage, 12),
                stability=stability,
                limitations=tuple(limitations),
            )
        )
    if unusable:
        raise FactorEvidenceError(
            "requested factor evidence is below coverage: " + ", ".join(unusable)
        )
    if not results:
        raise FactorEvidenceError("no factor produced observations")
    return tuple(results)


def build_common_factor_evidence_objects(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_sets: Sequence[ResearchObject],
    features: FactorFeatureSnapshot,
    *,
    factor_ids: Sequence[str],
    min_abs_z: float,
    min_coverage: float,
    min_direction_agreement: float,
    min_peer_observations: int = MIN_FACTOR_OBSERVATIONS,
    created_at: datetime | None = None,
) -> tuple[ResearchObject, ...]:
    """Persist peer-relative evidence with a closed research/snapshot/peer DAG."""

    payloads = build_common_factor_evidence(
        research,
        data_snapshot,
        peer_sets,
        features,
        factor_ids=factor_ids,
        min_abs_z=min_abs_z,
        min_coverage=min_coverage,
        min_direction_agreement=min_direction_agreement,
        min_peer_observations=min_peer_observations,
    )
    parent_refs = (
        research.ref(),
        data_snapshot.ref(),
        *(item.ref() for item in peer_sets),
    )
    return tuple(
        create_research_object(
            payload,
            owner_scope=research.owner_scope,
            parent_refs=parent_refs,
            created_at=created_at,
        )
        for payload in payloads
    )
