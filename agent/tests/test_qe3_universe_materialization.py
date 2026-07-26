"""QE3 source materialization and candidate-order stability tests.

The generated 300-row corpus mirrors the strict joined Tushare field contract;
it is deterministic test data, not a claim about live CSI300 membership.
"""

from __future__ import annotations

import socket
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research import (
    CSI300_EXPECTED_CONSTITUENTS,
    CSI300_TUSHARE_INDEX_CODE,
    Csi300TushareSourceBatch,
    DataSnapshotRef,
    ResearchSpec,
    UniverseError,
    build_csi300_tushare_source_batch,
    build_peer_set,
    create_research_object,
    load_csi300_tushare_source_batch,
    materialize_csi300_tushare_universe,
)

_AS_OF = date(2026, 7, 26)
_TRADE_DATE = date(2026, 7, 24)
_CAPTURED_AT = datetime(2026, 7, 25, 9, 30, tzinfo=timezone.utc)
_TARGET = "600000.SH"


def _source_rows() -> list[dict[str, object]]:
    standard_weight = Decimal("0.333333")
    final_weight = Decimal("100") - (
        standard_weight * (CSI300_EXPECTED_CONSTITUENTS - 1)
    )
    rows: list[dict[str, object]] = []
    for index in range(CSI300_EXPECTED_CONSTITUENTS):
        if index < 150:
            code = f"{600000 + index:06d}.SH"
            exchange = "SSE"
        else:
            code = f"{index - 149:06d}.SZ"
            exchange = "SZSE"
        rows.append(
            {
                "index_code": CSI300_TUSHARE_INDEX_CODE,
                "con_code": code,
                "trade_date": _TRADE_DATE,
                "weight": (
                    final_weight
                    if index == CSI300_EXPECTED_CONSTITUENTS - 1
                    else standard_weight
                ),
                "stock_basic_ts_code": code,
                "exchange": exchange,
                "list_status": "L",
                "list_date": date(2010, 1, 1),
                "delist_date": None,
            }
        )
    return rows


def _batch(
    rows: list[dict[str, object]] | None = None,
    *,
    captured_at: datetime = _CAPTURED_AT,
) -> Csi300TushareSourceBatch:
    return Csi300TushareSourceBatch(
        batch_id="qe3-csi300-tushare-contract-v1",
        source_version="tushare-pro-v1",
        as_of=_AS_OF,
        captured_at=captured_at,
        constituents=tuple(rows if rows is not None else _source_rows()),
    )


def _peer_set(universe, symbols: tuple[str, ...]):
    research = create_research_object(
        ResearchSpec(
            symbols=(_TARGET,),
            as_of=_AS_OF,
            lookback_days=(252,),
            candidate_universe="csi300@2026-07-26",
        ),
        created_at=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
    )
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="b" * 64,
            as_of=_AS_OF,
            start_date=date(2025, 7, 28),
            end_date=_AS_OF,
            adjustment="qfq",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in symbols},
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 26, 10, 1, tzinfo=timezone.utc),
    )
    return build_peer_set(
        research,
        data_snapshot,
        universe,
        target_symbol=_TARGET,
    )


def test_raw_tushare_exports_join_into_the_same_order_normalized_batch() -> None:
    rows = _source_rows()
    weight_rows = [
        {
            "index_code": row["index_code"],
            "con_code": row["con_code"],
            "trade_date": "20260724",
            "weight": str(row["weight"]),
        }
        for row in rows
    ]
    stock_rows = [
        {
            "ts_code": row["stock_basic_ts_code"],
            "exchange": row["exchange"],
            "list_status": row["list_status"],
            "list_date": "20100101",
            "delist_date": "",
        }
        for row in rows
    ]

    batch = build_csi300_tushare_source_batch(
        list(reversed(weight_rows)),
        list(reversed(stock_rows)),
        batch_id="qe3-csi300-tushare-contract-v1",
        source_version="tushare-pro-v1",
        as_of=_AS_OF,
        captured_at=_CAPTURED_AT,
    )

    assert batch == _batch()
    assert batch.index_weight_trade_date == _TRADE_DATE

    with pytest.raises(UniverseError, match="stock_basic metadata missing"):
        build_csi300_tushare_source_batch(
            weight_rows,
            stock_rows[:-1],
            batch_id="qe3-csi300-tushare-contract-v1",
            source_version="tushare-pro-v1",
            as_of=_AS_OF,
            captured_at=_CAPTURED_AT,
        )


def test_complete_tushare_batch_materializes_content_bound_300_member_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("CSI300 materializer attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    batch = _batch()
    universe = materialize_csi300_tushare_universe(batch)
    snapshot = universe.snapshot(_AS_OF)

    assert batch.provider_index_code == "399300.SZ"
    assert batch.canonical_index_symbol == "000300.SH"
    assert len(batch.constituents) == CSI300_EXPECTED_CONSTITUENTS
    assert len(snapshot.active_symbols) == CSI300_EXPECTED_CONSTITUENTS
    assert snapshot.active_symbols == tuple(sorted(snapshot.active_symbols))
    assert universe.snapshot(date(2026, 7, 27)).active_symbols == ()
    assert universe.source_version == (
        f"tushare-pro-v1+sha256.{batch.source_content_sha256}"
    )
    assert batch.source_content_sha256 in universe.source_version
    assert universe.fixture_id.endswith(batch.source_content_sha256[:16])


def test_source_batch_round_trip_is_strictly_local_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    batch = _batch()
    source_path = tmp_path / "csi300-source.json"
    source_path.write_text(batch.model_dump_json(indent=2), encoding="utf-8")

    loaded = load_csi300_tushare_source_batch(source_path)
    assert loaded == batch
    assert loaded.source_content_sha256 == batch.source_content_sha256

    linked = tmp_path / "linked-source.json"
    linked.symlink_to(source_path)
    with pytest.raises(ValueError, match="regular, non-symlink"):
        load_csi300_tushare_source_batch(linked)


def test_sm03_candidate_and_snapshot_input_order_preserve_peer_order() -> None:
    rows = _source_rows()
    first_batch = _batch(rows)
    second_batch = _batch(list(reversed(rows)))
    first_universe = materialize_csi300_tushare_universe(first_batch)
    second_universe = materialize_csi300_tushare_universe(second_batch)
    symbols = first_universe.snapshot(_AS_OF).active_symbols

    first_peers = _peer_set(first_universe, symbols)
    second_peers = _peer_set(second_universe, tuple(reversed(symbols)))

    assert first_batch.source_content_sha256 == second_batch.source_content_sha256
    assert first_universe.history_sha256 == second_universe.history_sha256
    assert first_universe.snapshot(_AS_OF) == second_universe.snapshot(_AS_OF)
    assert len(first_peers.members) == CSI300_EXPECTED_CONSTITUENTS - 1
    assert first_peers.members == tuple(sorted(first_peers.members))
    assert first_peers.members == second_peers.members
    assert first_peers.included_reasons == second_peers.included_reasons
    assert first_peers.excluded_reasons == second_peers.excluded_reasons
    assert first_peers.coverage == second_peers.coverage == 1.0
    assert first_peers.warnings == second_peers.warnings


def test_semantic_source_edit_changes_bound_universe_identity() -> None:
    first_batch = _batch()
    changed = first_batch.model_dump(mode="python")
    changed["constituents"][0]["weight"] += Decimal("0.01")
    changed["constituents"][1]["weight"] -= Decimal("0.01")
    second_batch = Csi300TushareSourceBatch.model_validate(changed)

    first_universe = materialize_csi300_tushare_universe(first_batch)
    second_universe = materialize_csi300_tushare_universe(second_batch)

    assert first_batch.source_content_sha256 != second_batch.source_content_sha256
    assert first_universe.history_sha256 != second_universe.history_sha256
    assert (
        first_universe.snapshot(_AS_OF).snapshot_sha256
        != second_universe.snapshot(_AS_OF).snapshot_sha256
    )


def test_incomplete_or_nonclosing_source_batch_fails_closed() -> None:
    incomplete = _source_rows()[:-1]
    with pytest.raises(ValidationError, match="exactly 300 constituents"):
        _batch(incomplete)

    nonclosing = _source_rows()
    nonclosing[0]["weight"] = Decimal("10")
    with pytest.raises(ValidationError, match="weights must close near 100"):
        _batch(nonclosing)


def test_future_capture_and_invalid_stock_basic_join_fail_closed() -> None:
    with pytest.raises(ValidationError, match="future source capture"):
        _batch(
            captured_at=datetime(
                2026,
                7,
                27,
                0,
                0,
                tzinfo=timezone.utc,
            )
        )

    invalid_join = _source_rows()
    invalid_join[0]["stock_basic_ts_code"] = "600999.SH"
    with pytest.raises(ValidationError, match="must match index_weight con_code"):
        _batch(invalid_join)
