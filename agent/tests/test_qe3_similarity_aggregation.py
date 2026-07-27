"""QE3 three-channel aggregation, sensitivity, and explanation tests."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.research import (
    ChannelWeights,
    DataSnapshotRef,
    PeerSet,
    ResearchSpec,
    SimilarityAggregationError,
    SimilarityRun,
    SimilaritySensitivityScenario,
    StockCandidate,
    build_three_channel_similarity,
    build_three_channel_similarity_object,
    create_research_object,
    validate_research_chain,
)

_AS_OF = date(2025, 6, 30)
_TARGET = "600000.SH"
_MEMBERS = ("600001.SH", "600002.SH", "600003.SH", "600004.SH")
_BASE_SCORES = {
    "business": {
        "600001.SH": 0.9,
        "600002.SH": 0.7,
        "600003.SH": 0.3,
    },
    "factor": {
        "600001.SH": 0.8,
        "600002.SH": 0.9,
        "600003.SH": 0.2,
        "600004.SH": 0.7,
    },
    "price_volume": {
        "600001.SH": 0.8,
        "600002.SH": 0.5,
        "600003.SH": 0.1,
        "600004.SH": 0.7,
    },
}
_BASE_WEIGHTS = ChannelWeights(business=0.3, factor=0.4, price_volume=0.3)


def _objects():
    research = create_research_object(
        ResearchSpec(
            symbols=(_TARGET,),
            as_of=_AS_OF,
            lookback_days=(20, 60, 252),
            candidate_universe="csi300@2025-06-30",
        ),
        created_at=datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
    )
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="9" * 64,
            as_of=_AS_OF,
            start_date=date(2024, 7, 1),
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=(_TARGET, *_MEMBERS),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={
                symbol: "fixture" for symbol in (_TARGET, *_MEMBERS)
            },
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 27, 8, 1, tzinfo=timezone.utc),
    )
    peer_set = create_research_object(
        PeerSet(
            research_spec_ref=research.ref(),
            data_snapshot_ref=data_snapshot.ref(),
            target_symbol=_TARGET,
            members=_MEMBERS,
            included_reasons={symbol: ("fixed_peer",) for symbol in _MEMBERS},
            excluded_reasons={_TARGET: ("target_symbol",)},
            coverage=1.0,
        ),
        parent_refs=(research.ref(), data_snapshot.ref()),
        created_at=datetime(2026, 7, 27, 8, 2, tzinfo=timezone.utc),
    )
    return research, data_snapshot, peer_set


def _channel_weights(channel: str) -> ChannelWeights:
    return ChannelWeights(
        business=1.0 if channel == "business" else 0.0,
        factor=1.0 if channel == "factor" else 0.0,
        price_volume=1.0 if channel == "price_volume" else 0.0,
    )


def _channel_result(
    objects,
    channel: str,
    scores: dict[str, float],
    *,
    marker: int,
    truncate: bool = False,
):
    research, data_snapshot, peer_set = objects
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates = tuple(
        StockCandidate(
            symbol=symbol,
            rank=rank,
            business_score=score if channel == "business" else None,
            factor_score=score if channel == "factor" else None,
            price_volume_score=score if channel == "price_volume" else None,
            combined_score=score,
            coverage=1.0,
            evidence=(
                f"{channel}_snapshot_sha256:{channel[0] * 64}",
                f"{channel}_source:{symbol}=fixture@qe3-{channel}-v1",
            ),
            counterevidence=(f"{channel}_gap:none_material",),
        )
        for rank, (symbol, score) in enumerate(ordered, start=1)
    )
    missing = set(_MEMBERS) - set(scores)
    excluded = {_TARGET: ("target_symbol",)}
    excluded.update(
        {symbol: (f"{channel}_feature_record_missing",) for symbol in missing}
    )
    if truncate:
        excluded[ordered[-1][0]] = (f"{channel}_rank_below_top_n:3",)
        candidates = candidates[:-1]
    payload = SimilarityRun(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        factor_evidence_refs=(),
        weights=_channel_weights(channel),
        candidates=candidates,
        excluded_symbols=excluded,
        sensitivity_notes=(f"fixture:{channel}:{marker}",),
    )
    return create_research_object(
        payload,
        parent_refs=(research.ref(), data_snapshot.ref(), peer_set.ref()),
        created_at=datetime(2026, 7, 27, 8, marker, tzinfo=timezone.utc),
    )


def _inputs():
    objects = _objects()
    results = {
        channel: _channel_result(
            objects,
            channel,
            scores,
            marker=3 + index,
        )
        for index, (channel, scores) in enumerate(_BASE_SCORES.items())
    }
    window_scores = dict(_BASE_SCORES["price_volume"])
    window_scores.update({"600001.SH": 0.2, "600002.SH": 1.0})
    window_price = _channel_result(
        objects,
        "price_volume",
        window_scores,
        marker=6,
    )
    scenarios = (
        SimilaritySensitivityScenario(
            scenario_id="factor_heavy",
            kind="weights",
            weights=ChannelWeights(business=0.1, factor=0.8, price_volume=0.1),
            business_result=results["business"],
            factor_result=results["factor"],
            price_volume_result=results["price_volume"],
        ),
        SimilaritySensitivityScenario(
            scenario_id="window_60d",
            kind="window",
            weights=_BASE_WEIGHTS,
            business_result=results["business"],
            factor_result=results["factor"],
            price_volume_result=window_price,
        ),
    )
    return objects, results, window_price, scenarios


def _build(*, min_coverage: float = 0.6, reverse_scenarios: bool = False):
    objects, results, _window_price, scenarios = _inputs()
    run = build_three_channel_similarity(
        *objects,
        results["business"],
        results["factor"],
        results["price_volume"],
        weights=_BASE_WEIGHTS,
        top_n=4,
        min_coverage=min_coverage,
        sensitivity_scenarios=(
            tuple(reversed(scenarios)) if reverse_scenarios else scenarios
        ),
    )
    return run


def test_three_channel_score_is_recomputable_and_missing_weight_is_visible() -> None:
    run = _build()
    candidates = {item.symbol: item for item in run.candidates}
    assert tuple(item.symbol for item in run.candidates) == (
        "600001.SH",
        "600002.SH",
        "600004.SH",
        "600003.SH",
    )
    first = candidates["600001.SH"]
    assert first.combined_score == pytest.approx(0.3 * 0.9 + 0.4 * 0.8 + 0.3 * 0.8)
    missing = candidates["600004.SH"]
    assert missing.business_score is None
    assert missing.combined_score == pytest.approx((0.4 * 0.7 + 0.3 * 0.7) / 0.7)
    assert missing.coverage == pytest.approx(0.7)
    assert any(
        value.startswith("missing_channel:business:")
        for value in missing.counterevidence
    )


def test_sm09_weight_and_window_sensitivity_is_deterministic_and_ranked() -> None:
    first = _build()
    second = _build(reverse_scenarios=True)
    assert first == second
    notes = first.sensitivity_notes
    assert any(
        value.startswith("sensitivity_scenario:factor_heavy:kind=weights")
        for value in notes
    )
    assert any(
        value.startswith("sensitivity_scenario:window_60d:kind=window")
        for value in notes
    )
    assert (
        "sensitivity_rank:factor_heavy:600001.SH:base=1;scenario=2;"
        "delta=+1;stability=0.666666666667"
    ) in notes
    assert any(
        value.startswith("sensitivity_summary:600001.SH:scenarios=2;")
        for value in notes
    )


def test_sm10_evidence_counterevidence_sources_and_components_match_scores() -> None:
    run = _build()
    for candidate in run.candidates:
        assert candidate.evidence
        assert candidate.counterevidence
        assert any(value.startswith("combined_component:") for value in candidate.evidence)
        for channel in ("business", "factor", "price_volume"):
            score = getattr(candidate, f"{channel}_score")
            if score is not None:
                assert any(
                    value.startswith(f"{channel}_evidence:{channel}_source:")
                    for value in candidate.evidence
                )
        contributions = [
            float(value.split("contribution=", 1)[1].split(";", 1)[0])
            for value in candidate.evidence
            if value.startswith("combined_component:")
        ]
        assert sum(contributions) == pytest.approx(candidate.combined_score)


def test_zero_weight_channel_keeps_its_score_and_source_without_contribution() -> None:
    objects, results, _window_price, scenarios = _inputs()
    zero_business_weights = ChannelWeights(
        business=0.0,
        factor=0.5,
        price_volume=0.5,
    )
    window_scenario = scenarios[1].model_copy(
        update={"weights": zero_business_weights}
    )
    run = build_three_channel_similarity(
        *objects,
        results["business"],
        results["factor"],
        results["price_volume"],
        weights=zero_business_weights,
        top_n=4,
        min_coverage=0.5,
        sensitivity_scenarios=(scenarios[0], window_scenario),
    )
    candidate = next(item for item in run.candidates if item.symbol == "600001.SH")
    assert candidate.business_score == 0.9
    assert any(
        value.startswith("business_evidence:business_source:")
        for value in candidate.evidence
    )
    assert not any(
        value.startswith("combined_component:business:")
        for value in candidate.evidence
    )


def test_low_combined_coverage_excludes_instead_of_zero_filling() -> None:
    run = _build(min_coverage=0.8)
    assert "600004.SH" not in {item.symbol for item in run.candidates}
    assert run.excluded_symbols["600004.SH"][0] == (
        "combined_coverage_below_min:0.700000"
    )


def test_combined_object_closes_all_base_and_sensitivity_run_parents() -> None:
    objects, results, window_price, scenarios = _inputs()
    combined = build_three_channel_similarity_object(
        *objects,
        results["business"],
        results["factor"],
        results["price_volume"],
        weights=_BASE_WEIGHTS,
        top_n=4,
        min_coverage=0.6,
        sensitivity_scenarios=scenarios,
        created_at=datetime(2026, 7, 27, 8, 7, tzinfo=timezone.utc),
    )
    validate_research_chain(
        (
            *objects,
            results["business"],
            results["factor"],
            results["price_volume"],
            window_price,
            combined,
        )
    )


def test_aggregation_fails_closed_without_both_sensitivity_kinds_or_full_runs() -> None:
    objects, results, _window_price, scenarios = _inputs()
    with pytest.raises(SimilarityAggregationError, match="both weight and window"):
        build_three_channel_similarity(
            *objects,
            results["business"],
            results["factor"],
            results["price_volume"],
            weights=_BASE_WEIGHTS,
            top_n=4,
            min_coverage=0.6,
            sensitivity_scenarios=scenarios[:1],
        )
    invalid_weight = SimilaritySensitivityScenario(
        scenario_id="factor_heavy",
        kind="weights",
        weights=scenarios[0].weights,
        business_result=scenarios[0].business_result,
        factor_result=scenarios[0].factor_result,
        price_volume_result=scenarios[1].price_volume_result,
    )
    with pytest.raises(SimilarityAggregationError, match="keep all channel inputs"):
        build_three_channel_similarity(
            *objects,
            results["business"],
            results["factor"],
            results["price_volume"],
            weights=_BASE_WEIGHTS,
            top_n=4,
            min_coverage=0.6,
            sensitivity_scenarios=(invalid_weight, scenarios[1]),
        )
    invalid_window = SimilaritySensitivityScenario(
        scenario_id="window_60d",
        kind="window",
        weights=_BASE_WEIGHTS,
        business_result=results["business"],
        factor_result=results["factor"],
        price_volume_result=results["price_volume"],
    )
    with pytest.raises(SimilarityAggregationError, match="change at least one"):
        build_three_channel_similarity(
            *objects,
            results["business"],
            results["factor"],
            results["price_volume"],
            weights=_BASE_WEIGHTS,
            top_n=4,
            min_coverage=0.6,
            sensitivity_scenarios=(scenarios[0], invalid_window),
        )
    truncated = _channel_result(
        objects,
        "factor",
        _BASE_SCORES["factor"],
        marker=8,
        truncate=True,
    )
    with pytest.raises(SimilarityAggregationError, match="truncated"):
        build_three_channel_similarity(
            *objects,
            results["business"],
            truncated,
            results["price_volume"],
            weights=_BASE_WEIGHTS,
            top_n=4,
            min_coverage=0.6,
            sensitivity_scenarios=scenarios,
        )
