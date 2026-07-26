"""QE3 SM-01/SM-02 tests for point-in-time CSI300 peer construction."""

from __future__ import annotations

import socket
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research import (
    DataSnapshotRef,
    PointInTimeUniverse,
    ResearchSpec,
    UniverseError,
    UniverseInstrument,
    UniverseMembership,
    build_peer_set,
    build_peer_set_object,
    create_research_object,
    load_point_in_time_universe,
    validate_research_chain,
)

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "research"
    / "qe3_csi300_universe_v1.json"
)
_AS_OF = date(2025, 6, 30)
_EXPECTED_HISTORY_SHA256 = "34d513534c0f9c1864388560c802d19f99cd549087dd0c1188a0043d8ad1c977"
_EXPECTED_SNAPSHOT_SHA256 = "0bcf25547b45ffbcec86275be90b8e54b0f20195d9ca7afd84988240fe35715a"
_DATA_SYMBOLS = (
    "600519.SH",
    "000858.SZ",
    "000568.SZ",
    "601318.SH",
    "600036.SH",
    "000001.SZ",
    "001289.SZ",
    "301999.SZ",
    "600999.SH",
    "510300.SH",
    "601999.SH",
    "688999.SH",
)


def _load() -> PointInTimeUniverse:
    return load_point_in_time_universe(_FIXTURE)


def _objects(*, candidate_universe: str = "csi300@2025-06-30"):
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH",),
            as_of=_AS_OF,
            lookback_days=(60, 120, 252),
            candidate_universe=candidate_universe,
        ),
        created_at=datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc),
    )
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="a" * 64,
            as_of=_AS_OF,
            start_date=date(2024, 7, 1),
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=_DATA_SYMBOLS,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in _DATA_SYMBOLS},
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 26, 8, 1, tzinfo=timezone.utc),
    )
    return research, data_snapshot


def test_qe3_universe_fixture_is_content_bound_and_strictly_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("universe fixture reader attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    universe = _load()
    point_in_time = universe.snapshot(_AS_OF)

    assert universe.fixture_id == "qe3-csi300-sm01-sm02-v1"
    assert universe.history_sha256 == _EXPECTED_HISTORY_SHA256
    assert point_in_time.snapshot_sha256 == _EXPECTED_SNAPSHOT_SHA256
    assert point_in_time.active_symbols == tuple(sorted(point_in_time.active_symbols))
    assert "688999.SH" not in point_in_time.active_symbols


def test_sm01_rebuilds_explicit_content_bound_peer_set_and_closed_dag() -> None:
    research, data_snapshot = _objects()
    universe = _load()

    peers = build_peer_set_object(
        research,
        data_snapshot,
        universe,
        target_symbol="600519.SH",
        created_at=datetime(2026, 7, 26, 8, 2, tzinfo=timezone.utc),
    )
    payload = peers.payload

    assert payload.members == (
        "000001.SZ",
        "000568.SZ",
        "000858.SZ",
        "600036.SH",
        "601318.SH",
    )
    assert payload.coverage == pytest.approx(5 / 6)
    assert set(payload.included_reasons) == set(payload.members)
    for reasons in payload.included_reasons.values():
        assert f"index_member:csi300@{_AS_OF.isoformat()}" in reasons
        assert f"data_snapshot_sha256:{'a' * 64}" in reasons
        assert f"universe_snapshot_sha256:{_EXPECTED_SNAPSHOT_SHA256}" in reasons
    assert f"universe_snapshot_sha256:{_EXPECTED_SNAPSHOT_SHA256}" in payload.warnings
    assert "eligible_data_coverage:5/6" in payload.warnings
    validate_research_chain((research, data_snapshot, peers))


def test_sm02_excludes_self_duplicates_future_and_ineligible_securities() -> None:
    research, data_snapshot = _objects()
    payload = build_peer_set(
        research,
        data_snapshot,
        _load(),
        target_symbol="600519.SH",
    )

    assert payload.excluded_reasons["600519.SH"] == ("target_symbol",)
    assert payload.excluded_reasons["001289.SZ"] == (
        "duplicate_security:000001.SZ",
    )
    assert payload.excluded_reasons["301999.SZ"] == (
        "not_listed_at_as_of:2025-07-01",
    )
    assert payload.excluded_reasons["600999.SH"] == (
        "delisted_at_as_of:2025-06-15",
    )
    assert payload.excluded_reasons["510300.SH"] == (
        "instrument_type_not_stock:etf",
    )
    assert payload.excluded_reasons["601999.SH"] == (
        "not_index_member_at_as_of:2025-06-30",
    )
    assert payload.excluded_reasons["688999.SH"] == (
        "not_index_member_at_as_of:2025-06-30",
    )
    assert payload.excluded_reasons["300999.SZ"] == (
        "missing_from_data_snapshot",
    )


def test_universe_and_input_order_do_not_change_snapshot_or_peer_identity() -> None:
    research, data_snapshot = _objects()
    universe = _load()
    reversed_universe = PointInTimeUniverse.model_validate(
        {
            **universe.model_dump(mode="json"),
            "instruments": list(reversed(universe.model_dump(mode="json")["instruments"])),
            "memberships": list(reversed(universe.model_dump(mode="json")["memberships"])),
        }
    )

    first = build_peer_set_object(
        research,
        data_snapshot,
        universe,
        target_symbol="600519.SH",
        created_at=datetime(2026, 7, 26, 8, 2, tzinfo=timezone.utc),
    )
    second = build_peer_set_object(
        research,
        data_snapshot,
        reversed_universe,
        target_symbol="600519.SH",
        created_at=datetime(2026, 7, 26, 9, 2, tzinfo=timezone.utc),
    )

    assert universe.snapshot(_AS_OF).snapshot_sha256 == reversed_universe.snapshot(
        _AS_OF
    ).snapshot_sha256
    assert first.object_id == second.object_id
    assert first.payload == second.payload


def test_future_known_facts_do_not_mutate_past_universe_snapshot() -> None:
    universe = _load()
    future_instrument = UniverseInstrument(
        security_id="CN688777",
        symbol="688777.SH",
        instrument_type="stock",
        listing_date=date(2025, 7, 2),
        primary_listing=True,
        known_at=datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc),
    )
    future_membership = UniverseMembership(
        source_row_id="m-688777-20250702",
        symbol="688777.SH",
        effective_from=date(2025, 7, 2),
        known_at=datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc),
    )
    mutated = PointInTimeUniverse.model_validate(
        {
            **universe.model_dump(mode="json"),
            "instruments": [
                *universe.model_dump(mode="json")["instruments"],
                future_instrument.model_dump(mode="json"),
            ],
            "memberships": [
                *universe.model_dump(mode="json")["memberships"],
                future_membership.model_dump(mode="json"),
            ],
        }
    )

    assert mutated.history_sha256 != universe.history_sha256
    assert mutated.snapshot(_AS_OF).snapshot_sha256 == universe.snapshot(
        _AS_OF
    ).snapshot_sha256


def test_universe_history_rejects_overlapping_membership_periods() -> None:
    universe = _load()
    data = universe.model_dump(mode="json")
    data["memberships"].append(
        {
            "source_row_id": "m-000858-overlap",
            "symbol": "000858.SZ",
            "effective_from": "2025-01-01",
            "effective_to": "2025-12-31",
            "known_at": "2024-12-20T17:00:00+08:00",
        }
    )

    with pytest.raises(ValidationError, match="membership periods overlap for 000858.SZ"):
        PointInTimeUniverse.model_validate(data)


def test_peer_builder_fails_closed_on_unbound_inputs_and_symlink(tmp_path: Path) -> None:
    research, data_snapshot = _objects(candidate_universe="csi300-current")
    with pytest.raises(UniverseError, match="candidate_universe must be"):
        build_peer_set(
            research,
            data_snapshot,
            _load(),
            target_symbol="600519.SH",
        )

    valid_research, valid_snapshot = _objects()
    unbound_snapshot = create_research_object(
        valid_snapshot.payload,
        created_at=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(UniverseError, match="does not descend from the research_spec"):
        build_peer_set(
            valid_research,
            unbound_snapshot,
            _load(),
            target_symbol="600519.SH",
        )

    linked = tmp_path / "universe.json"
    linked.symlink_to(_FIXTURE)
    with pytest.raises(UniverseError, match="regular, non-symlink"):
        load_point_in_time_universe(linked)
