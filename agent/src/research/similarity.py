"""Deterministic aggregation of the three independent QE3 similarity channels."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

CHANNEL_NAMES = ("business", "factor", "price_volume")


class SimilarityAggregationError(ValueError):
    """Raised when channel results cannot be safely combined."""


class SimilaritySensitivityScenario(BaseModel):
    """One content-bound weight or window perturbation for SM-09."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    kind: Literal["weights", "window"]
    weights: ChannelWeights
    business_result: ResearchObject
    factor_result: ResearchObject
    price_volume_result: ResearchObject


class _ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["business", "factor", "price_volume"]
    result: ResearchObject


class _AggregationInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channels: tuple[_ChannelInput, ...]
    scenarios: tuple[SimilaritySensitivityScenario, ...]

    @field_validator("channels")
    @classmethod
    def normalize_channels(
        cls,
        values: tuple[_ChannelInput, ...],
    ) -> tuple[_ChannelInput, ...]:
        result = tuple(sorted(values, key=lambda item: item.name))
        if tuple(item.name for item in result) != CHANNEL_NAMES:
            raise ValueError("exactly one result per similarity channel is required")
        return result

    @field_validator("scenarios")
    @classmethod
    def normalize_scenarios(
        cls,
        values: tuple[SimilaritySensitivityScenario, ...],
    ) -> tuple[SimilaritySensitivityScenario, ...]:
        result = tuple(sorted(values, key=lambda item: item.scenario_id))
        ids = tuple(item.scenario_id for item in result)
        if len(ids) != len(set(ids)):
            raise ValueError("sensitivity scenario IDs must be unique")
        kinds = {item.kind for item in result}
        if kinds != {"weights", "window"}:
            raise ValueError(
                "sensitivity scenarios require both weight and window perturbations"
            )
        return result


class _ScoredCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    business_score: float | None
    factor_score: float | None
    price_volume_score: float | None
    combined_score: float
    coverage: float
    evidence: tuple[str, ...]
    counterevidence: tuple[str, ...]


def _require_objects(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
) -> tuple[ResearchSpec, DataSnapshotRef, PeerSet]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload,
        ResearchSpec,
    ):
        raise SimilarityAggregationError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload,
        DataSnapshotRef,
    ):
        raise SimilarityAggregationError(
            "data_snapshot must be a data_snapshot_ref object"
        )
    if peer_set.object_type != "peer_set" or not isinstance(
        peer_set.payload,
        PeerSet,
    ):
        raise SimilarityAggregationError("peer_set must be a peer_set object")
    if len({research.owner_scope, data_snapshot.owner_scope, peer_set.owner_scope}) != 1:
        raise SimilarityAggregationError("aggregation inputs cross owner_scope boundaries")
    if research.ref() not in data_snapshot.parent_refs:
        raise SimilarityAggregationError(
            "data_snapshot does not descend from the research_spec"
        )
    if (
        research.ref() not in peer_set.parent_refs
        or data_snapshot.ref() not in peer_set.parent_refs
    ):
        raise SimilarityAggregationError(
            "peer_set does not descend from research and data snapshot"
        )
    peers = peer_set.payload
    if (
        peers.research_spec_ref != research.ref()
        or peers.data_snapshot_ref != data_snapshot.ref()
    ):
        raise SimilarityAggregationError("peer_set payload references do not match")
    return research.payload, data_snapshot.payload, peers


def _expected_weights(channel: str) -> ChannelWeights:
    return ChannelWeights(
        business=1.0 if channel == "business" else 0.0,
        factor=1.0 if channel == "factor" else 0.0,
        price_volume=1.0 if channel == "price_volume" else 0.0,
    )


def _validate_channel_result(
    result: ResearchObject,
    channel: str,
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    peers: PeerSet,
) -> SimilarityRun:
    if result.object_type != "similarity_run" or not isinstance(
        result.payload,
        SimilarityRun,
    ):
        raise SimilarityAggregationError(f"{channel} result is not a similarity_run")
    if result.owner_scope != research.owner_scope:
        raise SimilarityAggregationError(f"{channel} result crosses owner_scope")
    required_parents = {research.ref(), data_snapshot.ref(), peer_set.ref()}
    if not required_parents <= set(result.parent_refs):
        raise SimilarityAggregationError(
            f"{channel} result does not descend from the selected inputs"
        )
    run = result.payload
    if (
        run.research_spec_ref != research.ref()
        or run.data_snapshot_ref != data_snapshot.ref()
    ):
        raise SimilarityAggregationError(
            f"{channel} result references a different research snapshot"
        )
    if run.weights != _expected_weights(channel):
        raise SimilarityAggregationError(
            f"{channel} input must be an independent single-channel result"
        )
    candidate_symbols = {item.symbol for item in run.candidates}
    if candidate_symbols - set(peers.members):
        raise SimilarityAggregationError(f"{channel} result contains a non-peer symbol")
    unresolved = set(peers.members) - candidate_symbols - set(run.excluded_symbols)
    if unresolved:
        raise SimilarityAggregationError(
            f"{channel} result omits peer outcomes: {sorted(unresolved)}"
        )
    for reasons in run.excluded_symbols.values():
        if any("rank_below_top_n" in reason for reason in reasons):
            raise SimilarityAggregationError(
                f"{channel} input was truncated before aggregation"
            )
    score_field = f"{channel}_score"
    other_fields = {
        f"{item}_score" for item in CHANNEL_NAMES if item != channel
    }
    for candidate in run.candidates:
        score = getattr(candidate, score_field)
        if score is None:
            raise SimilarityAggregationError(
                f"{channel} candidate lacks its channel score"
            )
        if any(getattr(candidate, field) is not None for field in other_fields):
            raise SimilarityAggregationError(
                f"{channel} input contains cross-channel candidate scores"
            )
        if abs(candidate.combined_score - score) > 1e-12:
            raise SimilarityAggregationError(
                f"{channel} candidate combined score is inconsistent"
            )
    return run


def _validated_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    business_result: ResearchObject,
    factor_result: ResearchObject,
    price_volume_result: ResearchObject,
    base_weights: ChannelWeights,
    sensitivity_scenarios: Sequence[SimilaritySensitivityScenario],
) -> tuple[
    PeerSet,
    tuple[_ChannelInput, ...],
    tuple[SimilaritySensitivityScenario, ...],
]:
    _spec, _snapshot, peers = _require_objects(research, data_snapshot, peer_set)
    try:
        inputs = _AggregationInputs(
            channels=(
                _ChannelInput(name="business", result=business_result),
                _ChannelInput(name="factor", result=factor_result),
                _ChannelInput(name="price_volume", result=price_volume_result),
            ),
            scenarios=tuple(sensitivity_scenarios),
        )
    except ValueError as exc:
        raise SimilarityAggregationError(str(exc)) from exc
    for item in inputs.channels:
        _validate_channel_result(
            item.result,
            item.name,
            research,
            data_snapshot,
            peer_set,
            peers,
        )
    base_results = {item.name: item.result for item in inputs.channels}
    for scenario in inputs.scenarios:
        scenario_results = {
            "business": scenario.business_result,
            "factor": scenario.factor_result,
            "price_volume": scenario.price_volume_result,
        }
        same_results = all(
            scenario_results[channel].ref() == base_results[channel].ref()
            for channel in CHANNEL_NAMES
        )
        if scenario.kind == "weights":
            if not same_results:
                raise SimilarityAggregationError(
                    "weight sensitivity must keep all channel inputs fixed"
                )
            if scenario.weights == base_weights:
                raise SimilarityAggregationError(
                    "weight sensitivity must perturb the base weights"
                )
        else:
            if scenario.weights != base_weights:
                raise SimilarityAggregationError(
                    "window sensitivity must keep the base weights fixed"
                )
            if same_results:
                raise SimilarityAggregationError(
                    "window sensitivity must change at least one channel input"
                )
        for channel, result in (
            ("business", scenario.business_result),
            ("factor", scenario.factor_result),
            ("price_volume", scenario.price_volume_result),
        ):
            _validate_channel_result(
                result,
                channel,
                research,
                data_snapshot,
                peer_set,
                peers,
            )
    return peers, inputs.channels, inputs.scenarios


def _channel_weight(weights: ChannelWeights, channel: str) -> float:
    return float(getattr(weights, channel))


def _score_candidates(
    peers: PeerSet,
    channels: Sequence[_ChannelInput],
    weights: ChannelWeights,
    *,
    min_coverage: float,
    explain: bool,
) -> tuple[tuple[_ScoredCandidate, ...], dict[str, tuple[str, ...]]]:
    runs = {item.name: item.result.payload for item in channels}
    candidates = {
        channel: {item.symbol: item for item in run.candidates}
        for channel, run in runs.items()
    }
    excluded = {
        symbol: tuple(reasons)
        for symbol, reasons in sorted(peers.excluded_reasons.items())
    }
    scored: list[_ScoredCandidate] = []
    for symbol in sorted(peers.members):
        present = [
            (channel, candidates[channel][symbol])
            for channel in CHANNEL_NAMES
            if symbol in candidates[channel]
        ]
        available = [
            (channel, item)
            for channel, item in present
            if _channel_weight(weights, channel) > 0.0
        ]
        available_weight = sum(
            _channel_weight(weights, channel) for channel, _item in available
        )
        coverage = round(
            sum(
                _channel_weight(weights, channel) * item.coverage
                for channel, item in available
            ),
            12,
        )
        missing = tuple(
            channel
            for channel in CHANNEL_NAMES
            if _channel_weight(weights, channel) > 0.0
            and symbol not in candidates[channel]
        )
        if not available or coverage < min_coverage:
            reasons = [f"combined_coverage_below_min:{coverage:.6f}"]
            for channel in missing:
                channel_reasons = runs[channel].excluded_symbols.get(
                    symbol,
                    ("channel_candidate_missing",),
                )
                reasons.extend(
                    f"missing_channel:{channel}:{reason}" for reason in channel_reasons
                )
            excluded[symbol] = tuple(reasons)
            continue
        combined = round(
            sum(
                _channel_weight(weights, channel)
                * float(getattr(item, f"{channel}_score"))
                for channel, item in available
            )
            / available_weight,
            12,
        )
        channel_scores = {
            channel: (
                float(getattr(candidates[channel][symbol], f"{channel}_score"))
                if symbol in candidates[channel]
                else None
            )
            for channel in CHANNEL_NAMES
        }
        evidence: list[str] = []
        counterevidence: list[str] = []
        for channel, item in present:
            channel_weight = _channel_weight(weights, channel)
            score = float(getattr(item, f"{channel}_score"))
            if channel_weight > 0.0:
                effective_weight = channel_weight / available_weight
                evidence.append(
                    f"combined_component:{channel}:effective_weight="
                    f"{effective_weight:.12f};score={score:.12f};"
                    f"contribution={effective_weight * score:.12f};"
                    f"channel_coverage={item.coverage:.12f}"
                )
            if explain:
                evidence.extend(
                    f"{channel}_evidence:{value}" for value in item.evidence
                )
                counterevidence.extend(
                    f"{channel}_counterevidence:{value}"
                    for value in item.counterevidence
                )
        for channel in missing:
            channel_reasons = runs[channel].excluded_symbols.get(
                symbol,
                ("channel_candidate_missing",),
            )
            counterevidence.extend(
                f"missing_channel:{channel}:{reason}" for reason in channel_reasons
            )
        if not counterevidence:
            counterevidence.append("counterevidence:none_material_across_channels")
        scored.append(
            _ScoredCandidate(
                symbol=symbol,
                business_score=channel_scores["business"],
                factor_score=channel_scores["factor"],
                price_volume_score=channel_scores["price_volume"],
                combined_score=combined,
                coverage=coverage,
                evidence=tuple(evidence),
                counterevidence=tuple(counterevidence),
            )
        )
    ordered = tuple(
        sorted(
            scored,
            key=lambda item: (
                -item.combined_score,
                -item.coverage,
                item.symbol,
            ),
        )
    )
    return ordered, excluded


def _sensitivity_notes(
    peers: PeerSet,
    base_ordered: tuple[_ScoredCandidate, ...],
    scenarios: tuple[SimilaritySensitivityScenario, ...],
    *,
    min_coverage: float,
) -> tuple[str, ...]:
    base_ranks = {item.symbol: rank for rank, item in enumerate(base_ordered, start=1)}
    rank_count = max(1, len(peers.members) - 1)
    per_symbol: dict[str, list[float]] = {symbol: [] for symbol in base_ranks}
    notes: list[str] = []
    for scenario in scenarios:
        channels = tuple(
            sorted(
                (
                    _ChannelInput(name="business", result=scenario.business_result),
                    _ChannelInput(name="factor", result=scenario.factor_result),
                    _ChannelInput(
                        name="price_volume",
                        result=scenario.price_volume_result,
                    ),
                ),
                key=lambda item: item.name,
            )
        )
        ordered, _excluded = _score_candidates(
            peers,
            channels,
            scenario.weights,
            min_coverage=min_coverage,
            explain=False,
        )
        scenario_ranks = {
            item.symbol: rank for rank, item in enumerate(ordered, start=1)
        }
        digest = canonical_sha256(
            {
                "scenario_id": scenario.scenario_id,
                "kind": scenario.kind,
                "weights": scenario.weights,
                "result_refs": [item.result.ref() for item in channels],
            }
        )
        notes.append(
            f"sensitivity_scenario:{scenario.scenario_id}:kind={scenario.kind};"
            f"sha256={digest};weights={scenario.weights.business:.12f},"
            f"{scenario.weights.factor:.12f},{scenario.weights.price_volume:.12f}"
        )
        for symbol, base_rank in sorted(base_ranks.items()):
            scenario_rank = scenario_ranks.get(symbol)
            if scenario_rank is None:
                stability = 0.0
                rank_text = "excluded"
                delta_text = "excluded"
            else:
                delta = scenario_rank - base_rank
                stability = max(0.0, 1.0 - abs(delta) / rank_count)
                rank_text = str(scenario_rank)
                delta_text = f"{delta:+d}"
            per_symbol[symbol].append(stability)
            notes.append(
                f"sensitivity_rank:{scenario.scenario_id}:{symbol}:"
                f"base={base_rank};scenario={rank_text};delta={delta_text};"
                f"stability={stability:.12f}"
            )
    for symbol, values in sorted(per_symbol.items()):
        notes.append(
            f"sensitivity_summary:{symbol}:scenarios={len(values)};"
            f"mean_rank_stability={sum(values) / len(values):.12f}"
        )
    return tuple(notes)


def build_three_channel_similarity(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    business_result: ResearchObject,
    factor_result: ResearchObject,
    price_volume_result: ResearchObject,
    *,
    weights: ChannelWeights,
    top_n: int,
    min_coverage: float,
    sensitivity_scenarios: Sequence[SimilaritySensitivityScenario],
) -> SimilarityRun:
    """Combine complete single-channel runs with visible coverage and sensitivity."""

    if not 1 <= top_n <= 500:
        raise SimilarityAggregationError("top_n must be between 1 and 500")
    if not math.isfinite(min_coverage) or not 0.0 < min_coverage <= 1.0:
        raise SimilarityAggregationError("min_coverage must be in (0, 1]")
    peers, channels, scenarios = _validated_inputs(
        research,
        data_snapshot,
        peer_set,
        business_result,
        factor_result,
        price_volume_result,
        weights,
        sensitivity_scenarios,
    )
    ordered, excluded = _score_candidates(
        peers,
        channels,
        weights,
        min_coverage=min_coverage,
        explain=True,
    )
    if not ordered:
        raise SimilarityAggregationError(
            "no peer meets the minimum three-channel coverage"
        )
    selected = ordered[:top_n]
    for item in ordered[top_n:]:
        excluded[item.symbol] = (f"combined_rank_below_top_n:{top_n}",)
    candidates = tuple(
        StockCandidate(rank=rank, **item.model_dump())
        for rank, item in enumerate(selected, start=1)
    )
    base_hashes = tuple(
        f"channel_input_sha256:{item.name}:{canonical_sha256(item.result.payload)}"
        for item in channels
    )
    notes = (
        f"combined_weights:business={weights.business:.12f},"
        f"factor={weights.factor:.12f},"
        f"price_volume={weights.price_volume:.12f}",
        f"combined_min_coverage:{min_coverage:.6f}",
        "missing_channels:weight_renormalized_and_coverage_reduced",
        "combined_score:sum_of_visible_effective_weight_contributions",
        "ranking_tiebreakers:combined_score_desc,coverage_desc,symbol_asc",
        *base_hashes,
        *_sensitivity_notes(
            peers,
            ordered,
            scenarios,
            min_coverage=min_coverage,
        ),
    )
    factor_evidence_refs = tuple(
        sorted(
            {
                ref
                for item in channels
                for ref in item.result.payload.factor_evidence_refs
            },
            key=lambda ref: (ref.object_type, ref.object_id),
        )
    )
    return SimilarityRun(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        factor_evidence_refs=factor_evidence_refs,
        weights=weights,
        candidates=candidates,
        excluded_symbols=dict(sorted(excluded.items())),
        sensitivity_notes=notes,
    )


def build_three_channel_similarity_object(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    peer_set: ResearchObject,
    business_result: ResearchObject,
    factor_result: ResearchObject,
    price_volume_result: ResearchObject,
    *,
    weights: ChannelWeights,
    top_n: int,
    min_coverage: float,
    sensitivity_scenarios: Sequence[SimilaritySensitivityScenario],
    created_at: datetime | None = None,
) -> ResearchObject:
    """Persist a combined run descending from all base and scenario channel runs."""

    payload = build_three_channel_similarity(
        research,
        data_snapshot,
        peer_set,
        business_result,
        factor_result,
        price_volume_result,
        weights=weights,
        top_n=top_n,
        min_coverage=min_coverage,
        sensitivity_scenarios=sensitivity_scenarios,
    )
    result_objects = {
        item.object_id: item
        for item in (
            business_result,
            factor_result,
            price_volume_result,
            *(
                result
                for scenario in sensitivity_scenarios
                for result in (
                    scenario.business_result,
                    scenario.factor_result,
                    scenario.price_volume_result,
                )
            ),
        )
    }
    parent_refs = [
        research.ref(),
        data_snapshot.ref(),
        peer_set.ref(),
        *(item.ref() for item in result_objects.values()),
        *payload.factor_evidence_refs,
    ]
    return create_research_object(
        payload,
        owner_scope=research.owner_scope,
        parent_refs=parent_refs,
        created_at=created_at,
    )
