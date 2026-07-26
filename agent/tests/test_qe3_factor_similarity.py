"""QE3 factor-channel robust normalization and missing-data tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research import (
    DataSnapshotRef,
    FactorFeatureRecord,
    FactorFeatureSnapshot,
    FactorFeatureValue,
    FactorSimilarityError,
    PeerSet,
    ResearchSpec,
    build_factor_similarity,
    build_factor_similarity_object,
    create_research_object,
    load_factor_feature_snapshot,
    validate_research_chain,
)

_AS_OF = date(2025, 6, 30)
_KNOWN_AT = datetime(2025, 6, 30, 8, 0, tzinfo=timezone.utc)
_TARGET = "600000.SH"
_MEMBERS = (
    "600001.SH",
    "600002.SH",
    "600003.SH",
    "600004.SH",
    "600005.SH",
    "600006.SH",
)
_FACTOR_VALUES = {
    _TARGET: {"momentum_20d": 0.20, "volatility_20d": 0.30},
    "600001.SH": {"momentum_20d": 0.21, "volatility_20d": 0.31},
    "600002.SH": {"momentum_20d": 0.22, "volatility_20d": 0.32},
    "600003.SH": {"momentum_20d": 0.22, "volatility_20d": 0.32},
    "600004.SH": {"momentum_20d": -0.20, "volatility_20d": 0.80},
    "600005.SH": {"momentum_20d": 1_000.0, "volatility_20d": 100.0},
    "600006.SH": {"momentum_20d": 0.50},
}


def _value(factor_id: str, value: float) -> FactorFeatureValue:
    return FactorFeatureValue(
        factor_id=factor_id,
        value=value,
        source="fixture",
        source_version="qe3-factor-v1",
        known_at=_KNOWN_AT,
        source_fields=(f"computed.{factor_id}",),
    )


def _records(
    *,
    scale: float = 1.0,
    values: dict[str, dict[str, float]] | None = None,
) -> tuple[FactorFeatureRecord, ...]:
    source = values or _FACTOR_VALUES
    return tuple(
        FactorFeatureRecord(
            symbol=symbol,
            values=tuple(
                _value(factor_id, value * scale)
                for factor_id, value in factor_values.items()
            ),
        )
        for symbol, factor_values in source.items()
    )


def _snapshot(
    *,
    records: tuple[FactorFeatureRecord, ...] | None = None,
) -> FactorFeatureSnapshot:
    return FactorFeatureSnapshot(
        snapshot_id="qe3-factor-sm03-sm06-v1",
        as_of=_AS_OF,
        records=records or _records(),
    )


def _objects(
    *,
    members: tuple[str, ...] = _MEMBERS,
    snapshot_symbols: tuple[str, ...] | None = None,
):
    research = create_research_object(
        ResearchSpec(
            symbols=(_TARGET,),
            as_of=_AS_OF,
            lookback_days=(20, 252),
            candidate_universe="csi300@2025-06-30",
        ),
        created_at=datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc),
    )
    symbols = snapshot_symbols or (_TARGET, *members)
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="e" * 64,
            as_of=_AS_OF,
            start_date=date(2024, 7, 1),
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in symbols},
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 26, 13, 1, tzinfo=timezone.utc),
    )
    peers = create_research_object(
        PeerSet(
            research_spec_ref=research.ref(),
            data_snapshot_ref=data_snapshot.ref(),
            target_symbol=_TARGET,
            members=members,
            included_reasons={symbol: ("fixed_peer",) for symbol in members},
            excluded_reasons={_TARGET: ("target_symbol",)},
            coverage=1.0,
        ),
        parent_refs=(research.ref(), data_snapshot.ref()),
        created_at=datetime(2026, 7, 26, 13, 2, tzinfo=timezone.utc),
    )
    return research, data_snapshot, peers


def _signature(run) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            candidate.symbol,
            candidate.rank,
            candidate.factor_score,
            candidate.combined_score,
            candidate.coverage,
        )
        for candidate in run.candidates
    )


def _run(*objects, features: FactorFeatureSnapshot | None = None):
    return build_factor_similarity(
        *objects,
        features or _snapshot(),
        factor_weights={"momentum_20d": 1.0, "volatility_20d": 1.0},
        top_n=6,
        min_coverage=0.5,
    )


def test_factor_snapshot_is_strict_content_bound_order_normalized_and_local(
    tmp_path: Path,
) -> None:
    first = _snapshot()
    reversed_records = tuple(
        FactorFeatureRecord(
            symbol=record.symbol,
            values=tuple(reversed(record.values)),
        )
        for record in reversed(_records())
    )
    second = _snapshot(records=reversed_records)
    assert first == second
    assert first.snapshot_sha256 == second.snapshot_sha256

    path = tmp_path / "factor-features.json"
    path.write_text(first.model_dump_json(indent=2), encoding="utf-8")
    assert load_factor_feature_snapshot(path) == first

    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(FactorSimilarityError, match="regular, non-symlink"):
        load_factor_feature_snapshot(linked)


def test_factor_snapshot_rejects_future_or_nonfinite_values() -> None:
    future = _value("momentum_20d", 0.2).model_copy(
        update={"known_at": datetime(2025, 7, 1, tzinfo=timezone.utc)}
    )
    with pytest.raises(ValidationError, match="future-known factor"):
        _snapshot(
            records=(
                FactorFeatureRecord(symbol=_TARGET, values=(future,)),
                _records()[1],
            )
        )

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            _value("momentum_20d", value)


def test_factor_similarity_builds_explainable_ranked_closed_dag() -> None:
    research, data_snapshot, peers = _objects()
    result = build_factor_similarity_object(
        research,
        data_snapshot,
        peers,
        _snapshot(),
        factor_weights={"momentum_20d": 1.0, "volatility_20d": 1.0},
        top_n=6,
        min_coverage=0.5,
        created_at=datetime(2026, 7, 26, 13, 3, tzinfo=timezone.utc),
    )
    payload = result.payload

    assert payload.weights.factor == 1.0
    assert payload.weights.business == payload.weights.price_volume == 0.0
    assert payload.candidates[0].symbol == "600001.SH"
    assert payload.candidates[-1].symbol == "600005.SH"
    assert all(candidate.business_score is None for candidate in payload.candidates)
    assert all(
        candidate.price_volume_score is None for candidate in payload.candidates
    )
    assert all(
        candidate.evidence and candidate.counterevidence
        for candidate in payload.candidates
    )
    assert any(
        note.startswith("robust_stats:momentum_20d:")
        for note in payload.sensitivity_notes
    )
    assert (
        "factor_missing_values:weight_renormalized_never_zero_filled"
        in payload.sensitivity_notes
    )
    validate_research_chain((research, data_snapshot, peers, result))


def test_sm03_input_orders_preserve_factor_scores_and_ranking() -> None:
    first_inputs = _objects()
    reversed_members = tuple(reversed(_MEMBERS))
    second_inputs = _objects(
        members=reversed_members,
        snapshot_symbols=tuple(reversed((_TARGET, *reversed_members))),
    )
    first = _run(*first_inputs)
    reversed_records = tuple(
        FactorFeatureRecord(
            symbol=record.symbol,
            values=tuple(reversed(record.values)),
        )
        for record in reversed(_records())
    )
    second = _run(*second_inputs, features=_snapshot(records=reversed_records))

    assert _signature(first) == _signature(second)
    assert first.excluded_symbols == second.excluded_symbols
    assert first.sensitivity_notes == second.sensitivity_notes


def test_sm04_robust_normalization_keeps_outlier_last_and_near_neighbor_first() -> None:
    run = _run(*_objects())
    scores = {candidate.symbol: candidate.factor_score for candidate in run.candidates}

    assert run.candidates[0].symbol == "600001.SH"
    assert run.candidates[-1].symbol == "600005.SH"
    assert scores["600001.SH"] > scores["600004.SH"] > scores["600005.SH"]
    momentum_note = next(
        note
        for note in run.sensitivity_notes
        if note.startswith("robust_stats:momentum_20d:")
    )
    assert "median=0.22" in momentum_note
    assert "method=mad" in momentum_note


def test_sm05_equivalent_factor_scaling_and_ties_are_deterministic() -> None:
    objects = _objects()
    original = _run(*objects)
    scaled_snapshot = _snapshot(records=_records(scale=1_000.0))
    scaled = _run(*objects, features=scaled_snapshot)

    assert _snapshot().snapshot_sha256 != scaled_snapshot.snapshot_sha256
    assert _signature(original) == _signature(scaled)
    tied = [
        candidate.symbol
        for candidate in original.candidates
        if candidate.symbol in {"600002.SH", "600003.SH"}
    ]
    assert tied == ["600002.SH", "600003.SH"]


def test_sm06_missing_values_are_reweighted_and_low_coverage_is_visible() -> None:
    objects = _objects()
    accepted = _run(*objects)
    accepted_by_symbol = {
        candidate.symbol: candidate for candidate in accepted.candidates
    }
    partial = accepted_by_symbol["600006.SH"]

    assert partial.coverage == 0.5
    assert partial.factor_score is not None
    assert "missing_factor:volatility_20d:candidate" in partial.counterevidence

    excluded = build_factor_similarity(
        *objects,
        _snapshot(),
        factor_weights={"momentum_20d": 1.0, "volatility_20d": 1.0},
        top_n=6,
        min_coverage=0.75,
    )
    assert "600006.SH" not in {
        candidate.symbol for candidate in excluded.candidates
    }
    assert excluded.excluded_symbols["600006.SH"] == (
        "factor_coverage_below_min:0.500000",
        "missing_factor:volatility_20d:candidate",
    )


def test_factor_similarity_rejects_unbound_undercovered_or_invalid_inputs() -> None:
    research, data_snapshot, peers = _objects()
    alternate_snapshot = create_research_object(
        data_snapshot.payload.model_copy(update={"snapshot_sha256": "f" * 64}),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 26, 14, 0, tzinfo=timezone.utc),
    )
    unbound_peers = create_research_object(
        PeerSet(
            research_spec_ref=research.ref(),
            data_snapshot_ref=alternate_snapshot.ref(),
            target_symbol=peers.payload.target_symbol,
            members=peers.payload.members,
            included_reasons=peers.payload.included_reasons,
            excluded_reasons=peers.payload.excluded_reasons,
            coverage=peers.payload.coverage,
        ),
        parent_refs=(research.ref(), alternate_snapshot.ref()),
        created_at=datetime(2026, 7, 26, 14, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(FactorSimilarityError, match="does not descend"):
        _run(research, data_snapshot, unbound_peers)

    sparse_values = {
        _TARGET: {"momentum_20d": 0.2},
        "600001.SH": {"volatility_20d": 0.3},
    }
    with pytest.raises(FactorSimilarityError, match="no requested factor"):
        _run(
            research,
            data_snapshot,
            peers,
            features=_snapshot(records=_records(values=sparse_values)),
        )

    with pytest.raises(FactorSimilarityError, match="finite and positive"):
        build_factor_similarity(
            research,
            data_snapshot,
            peers,
            _snapshot(),
            factor_weights={"momentum_20d": float("nan")},
            top_n=5,
            min_coverage=0.5,
        )
