"""QE3 peer-relative common factor anomaly tests for SM-07."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.research import (
    DataSnapshotRef,
    FactorEvidenceError,
    FactorFeatureRecord,
    FactorFeatureSnapshot,
    FactorFeatureValue,
    PeerSet,
    ResearchSpec,
    build_common_factor_evidence,
    build_common_factor_evidence_objects,
    create_research_object,
    validate_research_chain,
)

_AS_OF = date(2025, 6, 30)
_KNOWN_AT = datetime(2025, 6, 30, 7, 0, tzinfo=timezone.utc)
_TARGETS = ("600000.SH", "000001.SZ")
_PEERS = {
    "600000.SH": (
        "600001.SH",
        "600002.SH",
        "600003.SH",
        "600004.SH",
        "600005.SH",
        "600006.SH",
        "600007.SH",
    ),
    "000001.SZ": ("000002.SZ", "000003.SZ", "000004.SZ"),
}
_COMMON = {
    "600000.SH": 10.0,
    "600001.SH": 0.0,
    "600002.SH": 1.0,
    "600003.SH": -1.0,
    "600004.SH": 0.5,
    "600005.SH": -0.5,
    "600006.SH": 0.2,
    "600007.SH": 1_000.0,
    "000001.SZ": 20.0,
    "000002.SZ": 10.0,
    "000003.SZ": 11.0,
    "000004.SZ": 9.0,
}
_MIXED = {
    "600000.SH": 5.0,
    "600001.SH": 0.0,
    "600002.SH": 1.0,
    "600003.SH": -1.0,
    "600004.SH": 0.5,
    "600005.SH": -0.5,
    "600006.SH": 0.2,
    "600007.SH": 0.1,
    "000001.SZ": 5.0,
    "000002.SZ": 10.0,
    "000003.SZ": 11.0,
    "000004.SZ": 9.0,
}


def _factor(factor_id: str, value: float) -> FactorFeatureValue:
    return FactorFeatureValue(
        factor_id=factor_id,
        value=value,
        source="fixture",
        source_version="qe3-common-anomaly-v1",
        known_at=_KNOWN_AT,
        source_fields=(f"computed.{factor_id}",),
    )


def _features(*, reverse: bool = False) -> FactorFeatureSnapshot:
    symbols = tuple(_COMMON)
    records = tuple(
        FactorFeatureRecord(
            symbol=symbol,
            values=(
                _factor("common_momentum", _COMMON[symbol]),
                _factor("mixed_value", _MIXED[symbol]),
            ),
        )
        for symbol in symbols
    )
    return FactorFeatureSnapshot(
        snapshot_id="qe3-sm07-common-anomaly-v1",
        as_of=_AS_OF,
        records=tuple(reversed(records)) if reverse else records,
    )


def _objects():
    symbols = tuple(_COMMON)
    research = create_research_object(
        ResearchSpec(
            symbols=_TARGETS,
            as_of=_AS_OF,
            lookback_days=(20, 60, 252),
            candidate_universe="peer-sets@2025-06-30",
        ),
        created_at=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
    )
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="8" * 64,
            as_of=_AS_OF,
            start_date=date(2024, 7, 1),
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=symbols,
            fields=("close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in symbols},
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 27, 9, 1, tzinfo=timezone.utc),
    )
    peer_sets = tuple(
        create_research_object(
            PeerSet(
                research_spec_ref=research.ref(),
                data_snapshot_ref=data_snapshot.ref(),
                target_symbol=target,
                members=members,
                included_reasons={symbol: ("own_industry_peer",) for symbol in members},
                excluded_reasons={target: ("target_symbol",)},
                coverage=1.0,
            ),
            parent_refs=(research.ref(), data_snapshot.ref()),
            created_at=datetime(
                2026,
                7,
                27,
                9,
                2 + index,
                tzinfo=timezone.utc,
            ),
        )
        for index, (target, members) in enumerate(_PEERS.items())
    )
    return research, data_snapshot, peer_sets


def _build(*, reverse: bool = False):
    research, data_snapshot, peer_sets = _objects()
    return build_common_factor_evidence(
        research,
        data_snapshot,
        tuple(reversed(peer_sets)) if reverse else peer_sets,
        _features(reverse=reverse),
        factor_ids=("mixed_value", "common_momentum"),
        min_abs_z=2.0,
        min_coverage=1.0,
        min_direction_agreement=0.75,
    )


def test_sm07_common_anomaly_uses_each_targets_own_peer_baseline() -> None:
    evidence = {item.factor_id: item for item in _build()}
    common = evidence["common_momentum"]
    assert common.direction == "positive"
    assert common.supporting_symbols == tuple(sorted(_TARGETS))
    assert common.contradicting_symbols == ()
    assert common.coverage == common.stability == 1.0
    observations = {item.symbol: item for item in common.observations}
    assert observations["600000.SH"].peer_median != observations["000001.SZ"].peer_median
    assert all(item.robust_zscore >= 2.0 for item in observations.values())
    assert any(
        value.startswith("peer_baseline:600000.SH:n=7;")
        for value in common.limitations
    )
    assert any(
        value.startswith("peer_baseline:000001.SZ:n=3;")
        for value in common.limitations
    )


def test_sm07_single_outlier_and_large_peer_group_do_not_dominate_targets() -> None:
    common = next(item for item in _build() if item.factor_id == "common_momentum")
    first = next(item for item in common.observations if item.symbol == "600000.SH")
    assert first.peer_median < 1.0
    assert first.robust_zscore > 2.0
    assert common.stability == 1.0


def test_sm07_mixed_direction_is_counterevidence_not_a_common_anomaly() -> None:
    mixed = next(item for item in _build() if item.factor_id == "mixed_value")
    assert mixed.direction == "mixed"
    assert mixed.supporting_symbols == ()
    assert mixed.contradicting_symbols == tuple(sorted(_TARGETS))
    assert mixed.stability == 0.0
    assert any(
        value.startswith("common_direction_below_threshold:")
        for value in mixed.limitations
    )


def test_common_evidence_is_order_stable_and_carries_source_provenance() -> None:
    first = _build()
    second = _build(reverse=True)
    assert first == second
    for item in first:
        for target in _TARGETS:
            assert any(
                value.startswith(f"factor_source:{item.factor_id}:{target}=")
                for value in item.limitations
            )
        assert any(
            value.startswith("factor_snapshot_sha256:")
            for value in item.limitations
        )
        for target in _TARGETS:
            assert any(
                value.startswith(f"peer_factor_sources:{item.factor_id}:{target}:")
                for value in item.limitations
            )


def test_common_evidence_objects_close_research_snapshot_and_both_peer_sets() -> None:
    research, data_snapshot, peer_sets = _objects()
    objects = build_common_factor_evidence_objects(
        research,
        data_snapshot,
        peer_sets,
        _features(),
        factor_ids=("common_momentum", "mixed_value"),
        min_abs_z=2.0,
        min_coverage=1.0,
        min_direction_agreement=0.75,
        created_at=datetime(2026, 7, 27, 9, 4, tzinfo=timezone.utc),
    )
    validate_research_chain((research, data_snapshot, *peer_sets, *objects))


def test_requested_factor_below_coverage_fails_closed_instead_of_disappearing() -> None:
    research, data_snapshot, peer_sets = _objects()
    records = tuple(
        FactorFeatureRecord(
            symbol=record.symbol,
            values=(
                tuple(
                    value
                    for value in record.values
                    if value.factor_id != "common_momentum"
                )
                if record.symbol == "000001.SZ"
                else record.values
            ),
        )
        for record in _features().records
    )
    incomplete = FactorFeatureSnapshot(
        snapshot_id="qe3-sm07-incomplete-v1",
        as_of=_AS_OF,
        records=records,
    )
    with pytest.raises(FactorEvidenceError, match="below coverage"):
        build_common_factor_evidence(
            research,
            data_snapshot,
            peer_sets,
            incomplete,
            factor_ids=("common_momentum", "mixed_value"),
            min_abs_z=2.0,
            min_coverage=1.0,
            min_direction_agreement=0.75,
        )


def test_common_evidence_rejects_duplicate_targets_and_low_peer_minimum() -> None:
    research, data_snapshot, peer_sets = _objects()
    with pytest.raises(FactorEvidenceError, match="unique target"):
        build_common_factor_evidence(
            research,
            data_snapshot,
            (peer_sets[0], peer_sets[0]),
            _features(),
            factor_ids=("common_momentum",),
            min_abs_z=2.0,
            min_coverage=1.0,
            min_direction_agreement=0.75,
        )
    with pytest.raises(FactorEvidenceError, match="at least 3"):
        build_common_factor_evidence(
            research,
            data_snapshot,
            peer_sets,
            _features(),
            factor_ids=("common_momentum",),
            min_abs_z=2.0,
            min_coverage=1.0,
            min_direction_agreement=0.75,
            min_peer_observations=2,
        )
