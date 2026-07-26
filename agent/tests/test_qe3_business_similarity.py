"""QE3 business-channel similarity and deterministic ranking tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research import (
    BusinessFeatureRecord,
    BusinessFeatureSnapshot,
    BusinessFieldProvenance,
    BusinessSimilarityError,
    DataSnapshotRef,
    PeerSet,
    ResearchSpec,
    build_business_similarity,
    build_business_similarity_object,
    build_tushare_business_feature_snapshot,
    create_research_object,
    load_business_feature_snapshot,
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


def _provenance(dimension: str) -> BusinessFieldProvenance:
    source_fields = {
        "industry": ("stock_basic.industry",),
        "market_cap": ("daily_basic.total_mv",),
        "liquidity": ("daily.amount",),
        "listing_age": ("stock_basic.list_date",),
    }
    return BusinessFieldProvenance(
        source="fixture",
        source_version="qe3-business-v1",
        known_at=_KNOWN_AT,
        source_fields=source_fields[dimension],
    )


def _record(
    symbol: str,
    *,
    industry: str | None,
    market_cap: float | None,
    turnover: float | None,
    listing_date: date | None,
) -> BusinessFeatureRecord:
    provenance = {}
    if industry is not None:
        provenance["industry"] = _provenance("industry")
    if market_cap is not None:
        provenance["market_cap"] = _provenance("market_cap")
    if turnover is not None:
        provenance["liquidity"] = _provenance("liquidity")
    if listing_date is not None:
        provenance["listing_age"] = _provenance("listing_age")
    return BusinessFeatureRecord(
        symbol=symbol,
        industry=industry,
        market_cap_cny=market_cap,
        average_daily_turnover_cny=turnover,
        liquidity_observation_count=20 if turnover is not None else None,
        listing_date=listing_date,
        provenance=provenance,
    )


def _records() -> tuple[BusinessFeatureRecord, ...]:
    return (
        _record(
            _TARGET,
            industry="consumer",
            market_cap=100_000_000_000.0,
            turnover=1_000_000_000.0,
            listing_date=date(2010, 1, 1),
        ),
        _record(
            "600001.SH",
            industry="consumer",
            market_cap=110_000_000_000.0,
            turnover=900_000_000.0,
            listing_date=date(2011, 1, 1),
        ),
        _record(
            "600002.SH",
            industry="consumer",
            market_cap=120_000_000_000.0,
            turnover=1_200_000_000.0,
            listing_date=date(2012, 1, 1),
        ),
        _record(
            "600003.SH",
            industry="consumer",
            market_cap=120_000_000_000.0,
            turnover=1_200_000_000.0,
            listing_date=date(2012, 1, 1),
        ),
        _record(
            "600004.SH",
            industry="bank",
            market_cap=10_000_000_000.0,
            turnover=100_000_000.0,
            listing_date=date(2025, 1, 1),
        ),
        _record(
            "600005.SH",
            industry="industrial",
            market_cap=1_000_000_000_000_000.0,
            turnover=1_000_000_000_000_000.0,
            listing_date=date(1990, 1, 1),
        ),
        _record(
            "600006.SH",
            industry="consumer",
            market_cap=None,
            turnover=None,
            listing_date=None,
        ),
    )


def _snapshot(
    records: tuple[BusinessFeatureRecord, ...] | None = None,
) -> BusinessFeatureSnapshot:
    return BusinessFeatureSnapshot(
        snapshot_id="qe3-business-sm03-sm05-v1",
        as_of=_AS_OF,
        liquidity_window_days=20,
        records=records if records is not None else _records(),
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
            lookback_days=(252,),
            candidate_universe="csi300@2025-06-30",
        ),
        created_at=datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
    )
    symbols = snapshot_symbols or (_TARGET, *members)
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="c" * 64,
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
        created_at=datetime(2026, 7, 26, 11, 1, tzinfo=timezone.utc),
    )
    peer_set = create_research_object(
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
        created_at=datetime(2026, 7, 26, 11, 2, tzinfo=timezone.utc),
    )
    return research, data_snapshot, peer_set


def _signature(run) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.symbol,
            item.rank,
            item.business_score,
            item.combined_score,
            item.coverage,
        )
        for item in run.candidates
    )


def test_business_snapshot_is_content_bound_order_normalized_and_local(
    tmp_path: Path,
) -> None:
    first = _snapshot()
    second = _snapshot(tuple(reversed(_records())))
    assert first == second
    assert first.snapshot_sha256 == second.snapshot_sha256

    path = tmp_path / "business-features.json"
    path.write_text(first.model_dump_json(indent=2), encoding="utf-8")
    assert load_business_feature_snapshot(path) == first

    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(BusinessSimilarityError, match="regular, non-symlink"):
        load_business_feature_snapshot(linked)


def test_business_snapshot_rejects_future_facts_and_missing_provenance() -> None:
    future = _provenance("industry").model_copy(
        update={
            "known_at": datetime(2025, 7, 1, 0, 0, tzinfo=timezone.utc),
        }
    )
    record = _record(
        _TARGET,
        industry="consumer",
        market_cap=None,
        turnover=None,
        listing_date=None,
    ).model_copy(update={"provenance": {"industry": future}})
    with pytest.raises(ValidationError, match="future-known industry"):
        _snapshot((record, _records()[1]))

    invalid = _records()[0].model_dump(mode="python")
    invalid["provenance"].pop("market_cap")
    with pytest.raises(ValidationError, match="cover populated business fields"):
        BusinessFeatureRecord.model_validate(invalid)


def test_tushare_business_adapter_scales_units_and_normalizes_raw_order() -> None:
    records = _records()
    stock_rows = [
        {
            "ts_code": record.symbol,
            "industry": record.industry or "",
            "list_status": "L",
            "list_date": (
                record.listing_date.strftime("%Y%m%d")
                if record.listing_date is not None
                else ""
            ),
        }
        for record in records
    ]
    cap_rows = [
        {
            "ts_code": record.symbol,
            "trade_date": "20250630",
            "total_mv": (
                record.market_cap_cny / 10_000
                if record.market_cap_cny is not None
                else None
            ),
        }
        for record in records
    ]
    daily_rows = [
        {
            "ts_code": record.symbol,
            "trade_date": trade_date,
            "amount": record.average_daily_turnover_cny / 1_000,
        }
        for record in records
        if record.average_daily_turnover_cny is not None
        for trade_date in ("20250627", "20250630")
    ]
    materialized = build_tushare_business_feature_snapshot(
        list(reversed(stock_rows)),
        list(reversed(cap_rows)),
        list(reversed(daily_rows)),
        symbols=tuple(reversed((_TARGET, *_MEMBERS))),
        snapshot_id="qe3-business-tushare-v1",
        source_version="tushare-pro-v1",
        as_of=_AS_OF,
        market_trade_date=_AS_OF,
        captured_at=_KNOWN_AT,
        liquidity_window_days=20,
    )
    by_symbol = {item.symbol: item for item in materialized.records}

    assert by_symbol[_TARGET].market_cap_cny == 100_000_000_000.0
    assert by_symbol[_TARGET].average_daily_turnover_cny == 1_000_000_000.0
    assert by_symbol[_TARGET].liquidity_observation_count == 2
    assert by_symbol[_TARGET].provenance["market_cap"].source_fields == (
        "daily_basic.total_mv",
    )
    assert materialized == build_tushare_business_feature_snapshot(
        stock_rows,
        cap_rows,
        daily_rows,
        symbols=(_TARGET, *_MEMBERS),
        snapshot_id="qe3-business-tushare-v1",
        source_version="tushare-pro-v1",
        as_of=_AS_OF,
        market_trade_date=_AS_OF,
        captured_at=_KNOWN_AT,
        liquidity_window_days=20,
    )

    invalid_caps = [dict(row) for row in cap_rows]
    invalid_caps[0]["trade_date"] = "20250627"
    with pytest.raises(BusinessSimilarityError, match="market_trade_date"):
        build_tushare_business_feature_snapshot(
            stock_rows,
            invalid_caps,
            daily_rows,
            symbols=(_TARGET, *_MEMBERS),
            snapshot_id="qe3-business-tushare-v1",
            source_version="tushare-pro-v1",
            as_of=_AS_OF,
            market_trade_date=_AS_OF,
            captured_at=_KNOWN_AT,
            liquidity_window_days=20,
        )


def test_business_similarity_builds_ranked_explainable_closed_dag() -> None:
    research, data_snapshot, peers = _objects()
    result = build_business_similarity_object(
        research,
        data_snapshot,
        peers,
        _snapshot(),
        top_n=5,
        min_coverage=0.5,
        created_at=datetime(2026, 7, 26, 11, 3, tzinfo=timezone.utc),
    )
    payload = result.payload

    assert payload.weights.business == 1.0
    assert payload.weights.factor == payload.weights.price_volume == 0.0
    assert payload.candidates[0].symbol == "600001.SH"
    assert payload.candidates[-1].symbol == "600004.SH"
    assert tuple(item.rank for item in payload.candidates) == (1, 2, 3, 4, 5)
    assert all(item.factor_score is None for item in payload.candidates)
    assert all(item.price_volume_score is None for item in payload.candidates)
    assert all(item.evidence and item.counterevidence for item in payload.candidates)
    assert payload.excluded_symbols["600006.SH"][0] == (
        "business_feature_coverage_below_min:1/4"
    )
    assert "business_missing_values:weight_renormalized_never_zero_filled" in (
        payload.sensitivity_notes
    )
    validate_research_chain((research, data_snapshot, peers, result))


def test_sm03_all_input_orders_preserve_scores_and_ranking() -> None:
    first_inputs = _objects()
    second_members = tuple(reversed(_MEMBERS))
    second_inputs = _objects(
        members=second_members,
        snapshot_symbols=tuple(reversed((_TARGET, *second_members))),
    )
    first = build_business_similarity(
        *first_inputs,
        _snapshot(),
        top_n=5,
        min_coverage=0.5,
    )
    second = build_business_similarity(
        *second_inputs,
        _snapshot(tuple(reversed(_records()))),
        top_n=5,
        min_coverage=0.5,
    )

    assert _signature(first) == _signature(second)
    assert first.excluded_symbols == second.excluded_symbols
    assert first.sensitivity_notes == second.sensitivity_notes


def test_sm04_near_and_reverse_order_survives_extreme_outlier() -> None:
    full_inputs = _objects()
    without_outlier_members = tuple(
        symbol for symbol in _MEMBERS if symbol != "600005.SH"
    )
    reduced_inputs = _objects(members=without_outlier_members)
    full = build_business_similarity(
        *full_inputs,
        _snapshot(),
        top_n=5,
        min_coverage=0.5,
    )
    reduced = build_business_similarity(
        *reduced_inputs,
        _snapshot(),
        top_n=4,
        min_coverage=0.5,
    )

    full_scores = {item.symbol: item.combined_score for item in full.candidates}
    reduced_scores = {item.symbol: item.combined_score for item in reduced.candidates}
    assert full.candidates[0].symbol == reduced.candidates[0].symbol == "600001.SH"
    assert full_scores["600001.SH"] == reduced_scores["600001.SH"]
    assert full_scores["600004.SH"] == reduced_scores["600004.SH"]
    assert full_scores["600001.SH"] > full_scores["600004.SH"]


def test_sm05_equivalent_scale_and_ties_do_not_change_ranking() -> None:
    inputs = _objects()
    original = build_business_similarity(
        *inputs,
        _snapshot(),
        top_n=5,
        min_coverage=0.5,
    )
    scaled_records = tuple(
        record.model_copy(
            update={
                "market_cap_cny": (
                    record.market_cap_cny * 1_000
                    if record.market_cap_cny is not None
                    else None
                ),
                "average_daily_turnover_cny": (
                    record.average_daily_turnover_cny * 1_000
                    if record.average_daily_turnover_cny is not None
                    else None
                ),
            }
        )
        for record in _records()
    )
    scaled_snapshot = _snapshot(scaled_records)
    scaled = build_business_similarity(
        *inputs,
        scaled_snapshot,
        top_n=5,
        min_coverage=0.5,
    )

    assert _snapshot().snapshot_sha256 != scaled_snapshot.snapshot_sha256
    assert _signature(original) == _signature(scaled)
    tied = [
        item.symbol
        for item in original.candidates
        if item.symbol in {"600002.SH", "600003.SH"}
    ]
    assert tied == ["600002.SH", "600003.SH"]


def test_unbound_or_undercovered_business_inputs_fail_closed() -> None:
    research, data_snapshot, peers = _objects()
    alternate_snapshot = create_research_object(
        data_snapshot.payload.model_copy(update={"snapshot_sha256": "d" * 64}),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
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
        created_at=datetime(2026, 7, 26, 12, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(BusinessSimilarityError, match="does not descend"):
        build_business_similarity(
            research,
            data_snapshot,
            unbound_peers,
            _snapshot(),
            top_n=5,
            min_coverage=0.5,
        )

    sparse_records = tuple(
        _record(
            symbol,
            industry="consumer",
            market_cap=None,
            turnover=None,
            listing_date=None,
        )
        for symbol in (_TARGET, *_MEMBERS)
    )
    with pytest.raises(BusinessSimilarityError, match="no peer meets"):
        build_business_similarity(
            research,
            data_snapshot,
            peers,
            _snapshot(sparse_records),
            top_n=5,
            min_coverage=1.0,
        )
