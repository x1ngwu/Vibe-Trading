"""QE3 qfq price/volume path similarity and point-in-time tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError

from src.research import (
    DataSnapshotRef,
    PeerSet,
    PriceVolumeFeatureRecord,
    PriceVolumeFeatureSnapshot,
    PriceVolumeObservation,
    PriceVolumeSimilarityError,
    ResearchSpec,
    build_price_volume_feature_snapshot_from_envelope,
    build_price_volume_similarity,
    build_price_volume_similarity_object,
    create_research_object,
    load_price_volume_feature_snapshot,
    validate_research_chain,
)

_AS_OF = date(2025, 6, 27)
_DATES = tuple(date(2025, 6, day) for day in (23, 24, 25, 26, 27))
_TARGET = "600000.SH"
_MEMBERS = (
    "600001.SH",
    "600002.SH",
    "600003.SH",
    "600004.SH",
    "600005.SH",
    "600006.SH",
)
_METRIC_WEIGHTS = {
    "price_path": 1.0,
    "return_correlation": 1.0,
    "volatility": 1.0,
    "drawdown": 1.0,
    "volume_path": 1.0,
    "turnover_path": 1.0,
    "price_volume_correlation": 1.0,
}
_PATHS = {
    _TARGET: (
        (100.0, 102.0, 101.0, 103.0, 105.0),
        (100.0, 120.0, 90.0, 140.0, 160.0),
    ),
    "600001.SH": (
        (10.0, 10.2, 10.1, 10.3, 10.5),
        (1_000.0, 1_200.0, 900.0, 1_400.0, 1_600.0),
    ),
    "600002.SH": (
        (200.0, 204.0, 202.0, 206.0, 210.0),
        (200.0, 240.0, 180.0, 280.0, 320.0),
    ),
    "600003.SH": (
        (100.0, 98.0, 99.0, 97.0, 95.0),
        (160.0, 140.0, 90.0, 120.0, 100.0),
    ),
    "600004.SH": (
        (100.0, 200.0, 50.0, 300.0, 10.0),
        (1.0, 10_000.0, 2.0, 20_000.0, 1.0),
    ),
    "600005.SH": (
        (100.0, 102.0, 101.0, 103.0, 105.0),
        (0.0, 0.0, 0.0, 0.0, 0.0),
    ),
    "600006.SH": (
        (100.0, 102.0, 103.0, 105.0),
        (100.0, 120.0, 140.0, 160.0),
    ),
}


def _record(
    symbol: str,
    *,
    price_scale: float = 1.0,
    activity_scale: float = 1.0,
) -> PriceVolumeFeatureRecord:
    closes, volumes = _PATHS[symbol]
    dates = _DATES
    suspensions: tuple[date, ...] = ()
    if symbol == "600006.SH":
        dates = (_DATES[0], _DATES[1], _DATES[3], _DATES[4])
        suspensions = (_DATES[2],)
    return PriceVolumeFeatureRecord(
        symbol=symbol,
        source="fixture",
        source_version="qe3-price-volume-v1",
        observations=tuple(
            PriceVolumeObservation(
                trade_date=trade_date,
                close=close * price_scale,
                volume=volume * activity_scale,
                amount=volume * activity_scale * 1_000.0,
            )
            for trade_date, close, volume in zip(dates, closes, volumes)
        ),
        suspension_dates=suspensions,
    )


def _records(
    *,
    price_scale: float = 1.0,
    activity_scale: float = 1.0,
) -> tuple[PriceVolumeFeatureRecord, ...]:
    return tuple(
        _record(
            symbol,
            price_scale=price_scale,
            activity_scale=activity_scale,
        )
        for symbol in (_TARGET, *_MEMBERS)
    )


def _snapshot(
    *,
    records: tuple[PriceVolumeFeatureRecord, ...] | None = None,
) -> PriceVolumeFeatureSnapshot:
    return PriceVolumeFeatureSnapshot(
        snapshot_id="qe3-price-volume-sm03-sm08-v1",
        data_snapshot_sha256="a" * 64,
        as_of=_AS_OF,
        window_days=5,
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
            lookback_days=(5, 20, 252),
            candidate_universe="csi300@2025-06-27",
        ),
        created_at=datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc),
    )
    symbols = snapshot_symbols or (_TARGET, *members)
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="a" * 64,
            as_of=_AS_OF,
            start_date=_DATES[0],
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in symbols},
            anomalies=("suspension:600006.SH:2025-06-25",),
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 27, 7, 1, tzinfo=timezone.utc),
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
        created_at=datetime(2026, 7, 27, 7, 2, tzinfo=timezone.utc),
    )
    return research, data_snapshot, peers


def _run(*objects, features: PriceVolumeFeatureSnapshot | None = None):
    return build_price_volume_similarity(
        *objects,
        features or _snapshot(),
        metric_weights=_METRIC_WEIGHTS,
        top_n=6,
        min_coverage=0.5,
    )


def _signature(run) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.symbol,
            item.rank,
            item.price_volume_score,
            item.combined_score,
            item.coverage,
        )
        for item in run.candidates
    )


def test_price_volume_snapshot_is_content_bound_order_normalized_and_local(
    tmp_path: Path,
) -> None:
    first = _snapshot()
    second = _snapshot(records=tuple(reversed(_records())))
    assert first == second
    assert first.snapshot_sha256 == second.snapshot_sha256

    path = tmp_path / "price-volume.json"
    path.write_text(first.model_dump_json(indent=2), encoding="utf-8")
    assert load_price_volume_feature_snapshot(path) == first

    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(PriceVolumeSimilarityError, match="regular, non-symlink"):
        load_price_volume_feature_snapshot(linked)


def test_envelope_adapter_binds_qfq_sources_and_suspensions() -> None:
    symbols = (_TARGET, "600006.SH")
    snapshot_ref = DataSnapshotRef(
        snapshot_sha256="b" * 64,
        as_of=_AS_OF,
        start_date=_DATES[0],
        end_date=_AS_OF,
        adjustment="qfq",
        symbols=symbols,
        fields=("close", "volume", "amount"),
        requested_sources=("fixture",),
        actual_sources={symbol: "fixture" for symbol in symbols},
    )
    frames = {}
    for symbol in symbols:
        record = _record(symbol)
        frames[symbol] = pd.DataFrame(
            {
                "close": [item.close for item in record.observations],
                "volume": [item.volume for item in record.observations],
                "amount": [item.amount for item in record.observations],
            },
            index=pd.to_datetime(
                [item.trade_date for item in record.observations]
            ),
        )
    envelope = SimpleNamespace(
        request=SimpleNamespace(
            adjustment="qfq",
            end_date=_AS_OF,
            symbols=symbols,
        ),
        manifest=SimpleNamespace(
            snapshot_sha256="b" * 64,
            actual_sources={symbol: "fixture" for symbol in symbols},
            source_versions={"fixture": "qe2-fixture-v1"},
            availability=(
                SimpleNamespace(
                    symbol="600006.SH",
                    trade_date=_DATES[2],
                    classification="suspension",
                ),
            ),
        ),
        frames=frames,
    )
    result = build_price_volume_feature_snapshot_from_envelope(
        snapshot_ref,
        envelope,
        window_days=5,
        snapshot_id="qe3-envelope-adapter-v1",
    )
    records = {item.symbol: item for item in result.records}
    assert result.data_snapshot_sha256 == snapshot_ref.snapshot_sha256
    assert records["600006.SH"].suspension_dates == (_DATES[2],)
    assert records[_TARGET].source_version == "qe2-fixture-v1"


def test_sm03_input_orders_preserve_price_volume_scores_and_ranking() -> None:
    first = _run(*_objects())
    reversed_members = tuple(reversed(_MEMBERS))
    second_objects = _objects(
        members=reversed_members,
        snapshot_symbols=tuple(reversed((_TARGET, *reversed_members))),
    )
    reversed_records = tuple(
        PriceVolumeFeatureRecord(
            symbol=record.symbol,
            source=record.source,
            source_version=record.source_version,
            source_fields=tuple(reversed(record.source_fields)),
            observations=tuple(reversed(record.observations)),
            suspension_dates=tuple(reversed(record.suspension_dates)),
        )
        for record in reversed(_records())
    )
    second = _run(*second_objects, features=_snapshot(records=reversed_records))
    assert _signature(first) == _signature(second)
    assert first.excluded_symbols == second.excluded_symbols
    assert first.sensitivity_notes == second.sensitivity_notes


def test_sm04_near_reverse_and_outlier_paths_rank_as_expected() -> None:
    run = _run(*_objects())
    scores = {item.symbol: item.price_volume_score for item in run.candidates}
    assert run.candidates[0].symbol == "600001.SH"
    assert scores["600001.SH"] > scores["600003.SH"]
    assert scores["600003.SH"] > scores["600004.SH"]
    assert run.candidates[-1].symbol == "600004.SH"


def test_sm05_equivalent_price_activity_scaling_and_ties_are_stable() -> None:
    objects = _objects()
    original = _run(*objects)
    scaled_snapshot = _snapshot(records=_records(price_scale=100.0, activity_scale=1_000.0))
    scaled = _run(*objects, features=scaled_snapshot)
    assert _snapshot().snapshot_sha256 != scaled_snapshot.snapshot_sha256
    assert _signature(original) == _signature(scaled)
    tied = [
        item.symbol
        for item in original.candidates
        if item.symbol in {"600001.SH", "600002.SH"}
    ]
    assert tied == ["600001.SH", "600002.SH"]


def test_sm06_missing_activity_reweights_and_low_coverage_is_visible() -> None:
    objects = _objects()
    accepted = _run(*objects)
    accepted_by_symbol = {item.symbol: item for item in accepted.candidates}
    missing_activity = accepted_by_symbol["600005.SH"]
    assert 0.5 <= missing_activity.coverage < 1.0
    assert missing_activity.price_volume_score is not None
    assert any(
        item.startswith("missing_price_volume_metric:volume_path")
        for item in missing_activity.counterevidence
    )

    excluded = build_price_volume_similarity(
        *objects,
        _snapshot(),
        metric_weights=_METRIC_WEIGHTS,
        top_n=6,
        min_coverage=0.75,
    )
    assert "600005.SH" not in {item.symbol for item in excluded.candidates}
    assert excluded.excluded_symbols["600005.SH"][0].startswith(
        "price_volume_coverage_below_min:"
    )


def test_sm08_qfq_and_suspension_boundaries_fail_closed_or_remain_visible() -> None:
    research, data_snapshot, peers = _objects()
    run = _run(research, data_snapshot, peers)
    suspended = next(item for item in run.candidates if item.symbol == "600006.SH")
    assert suspended.coverage < 1.0
    assert "suspension_mismatch:1" in suspended.counterevidence
    assert "missing_dates:not_forward_filled" in run.sensitivity_notes

    raw_snapshot = create_research_object(
        data_snapshot.payload.model_copy(update={"adjustment": "raw"}),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 27, 7, 3, tzinfo=timezone.utc),
    )
    raw_peers = create_research_object(
        peers.payload.model_copy(
            update={
                "data_snapshot_ref": raw_snapshot.ref(),
            }
        ),
        parent_refs=(research.ref(), raw_snapshot.ref()),
        created_at=datetime(2026, 7, 27, 7, 4, tzinfo=timezone.utc),
    )
    with pytest.raises(PriceVolumeSimilarityError, match="requires qfq"):
        build_price_volume_similarity(
            research,
            raw_snapshot,
            raw_peers,
            _snapshot(),
            metric_weights=_METRIC_WEIGHTS,
            top_n=6,
            min_coverage=0.5,
        )

    future = _record(_TARGET).model_copy(
        update={
            "observations": (
                *_record(_TARGET).observations[:-1],
                PriceVolumeObservation(
                    trade_date=date(2025, 6, 30),
                    close=106.0,
                    volume=180.0,
                    amount=180_000.0,
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="exceeds as_of"):
        _snapshot(records=(future, _record("600001.SH")))


def test_price_volume_result_is_explainable_closed_dag_and_rejects_bad_binding() -> None:
    research, data_snapshot, peers = _objects()
    result = build_price_volume_similarity_object(
        research,
        data_snapshot,
        peers,
        _snapshot(),
        metric_weights=_METRIC_WEIGHTS,
        top_n=6,
        min_coverage=0.5,
        created_at=datetime(2026, 7, 27, 7, 5, tzinfo=timezone.utc),
    )
    payload = result.payload
    assert payload.weights.price_volume == 1.0
    assert payload.weights.business == payload.weights.factor == 0.0
    assert all(item.evidence and item.counterevidence for item in payload.candidates)
    validate_research_chain((research, data_snapshot, peers, result))

    mismatched = _snapshot().model_copy(update={"data_snapshot_sha256": "f" * 64})
    with pytest.raises(PriceVolumeSimilarityError, match="do not bind"):
        _run(research, data_snapshot, peers, features=mismatched)
