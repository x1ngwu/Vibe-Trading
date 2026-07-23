"""QE2 DT-01/03/04/07/09 tests for the opt-in loader envelope."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest

from backtest.loaders.data_envelope import (
    DataFetchRequest,
    DuplicateConflictError,
    IncompleteDataError,
    LoaderCapability,
    UnitConflictError,
    fetch_data_envelope,
    make_downstream_cache_key,
    read_offline_snapshot,
    write_offline_snapshot,
)

_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_STOCK_UNITS = {
    "open": "CNY/share",
    "high": "CNY/share",
    "low": "CNY/share",
    "close": "CNY/share",
    "volume": "share",
    "amount": "CNY",
}
_INDEX_UNITS = {
    "open": "index_point",
    "high": "index_point",
    "low": "index_point",
    "close": "index_point",
    "volume": "provider_native_volume",
    "amount": "CNY",
}


def _capability(source: str, *, adjustments=("raw",)) -> LoaderCapability:
    return LoaderCapability(
        source=source,
        version="fixture-v1",
        instrument_types=("stock", "etf", "index"),
        intervals=("1D",),
        adjustments=adjustments,
        fields=_FIELDS,
        field_units={"stock": _STOCK_UNITS, "etf": _STOCK_UNITS, "index": _INDEX_UNITS},
    )


def _frame(close: float, *, duplicate: str | None = None) -> pd.DataFrame:
    index = ["2025-01-02", "2025-01-03"]
    values = {
        "open": [10.0, 10.5],
        "high": [10.5, max(11.0, close + 0.2)],
        "low": [9.8, min(10.3, close - 0.2)],
        "close": [10.2, close],
        "volume": [1000, 1200],
        "amount": [10100.0, 12800.0],
    }
    frame = pd.DataFrame(values, index=pd.DatetimeIndex(index, name="trade_date"))
    if duplicate is None:
        return frame
    duplicate_row = frame.iloc[[1]].copy()
    if duplicate == "conflict":
        duplicate_row.loc[:, "close"] = close + 0.1
        duplicate_row.loc[:, "amount"] = 12920.0
    return pd.concat([frame.iloc[[1]], frame.iloc[[0]], duplicate_row])


@dataclass
class _FakeLoader:
    name: str
    frames: dict[str, pd.DataFrame]
    errors: dict[str, Exception] = field(default_factory=dict)
    available: bool = True
    calls: list[str] = field(default_factory=list)

    def is_available(self) -> bool:
        return self.available

    def fetch(self, codes, *_args, **_kwargs):
        symbol = codes[0]
        self.calls.append(symbol)
        if symbol in self.errors:
            raise self.errors[symbol]
        frame = self.frames.get(symbol)
        return {} if frame is None else {symbol: frame}


def _request(*, adjustment="raw") -> DataFetchRequest:
    return DataFetchRequest(
        symbols=("600001.SH", "600002.SH", "000300.SH"),
        instrument_types={
            "600001.SH": "stock",
            "600002.SH": "stock",
            "000300.SH": "index",
        },
        start_date="2025-01-02",
        end_date="2025-01-03",
        adjustment=adjustment,
        fields=_FIELDS,
        requested_sources=("primary", "fallback"),
    )


def test_dt01_dt04_manifest_discloses_per_symbol_fallback_without_shrinking() -> None:
    primary = _FakeLoader("primary", {"600001.SH": _frame(10.8)})
    fallback = _FakeLoader(
        "fallback",
        {"600002.SH": _frame(9.8), "000300.SH": _frame(4010.0)},
    )

    envelope = fetch_data_envelope(
        _request(),
        loaders={"primary": primary, "fallback": fallback},
        capabilities={"primary": _capability("primary"), "fallback": _capability("fallback")},
    ).require_complete()

    assert tuple(envelope.frames) == ("600001.SH", "600002.SH", "000300.SH")
    assert envelope.manifest.requested_sources == ("primary", "fallback")
    assert envelope.manifest.source_versions == {
        "primary": "fixture-v1",
        "fallback": "fixture-v1",
    }
    assert envelope.manifest.actual_sources == {
        "600001.SH": "primary",
        "600002.SH": "fallback",
        "000300.SH": "fallback",
    }
    assert envelope.manifest.units["600001.SH"]["close"] == "CNY/share"
    assert envelope.manifest.units["000300.SH"]["close"] == "index_point"
    assert {item.kind for item in envelope.manifest.anomalies} == {"empty_result"}


def test_dt03_identical_duplicate_is_deterministic_but_conflict_fails_closed() -> None:
    identical = _FakeLoader("primary", {"600001.SH": _frame(10.8, duplicate="same")})
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    envelope = fetch_data_envelope(
        request,
        loaders={"primary": identical},
        capabilities={"primary": _capability("primary")},
    )

    assert len(envelope.frames["600001.SH"]) == 2
    assert [item.kind for item in envelope.manifest.anomalies] == ["duplicate_same"]

    conflicting = _FakeLoader("primary", {"600001.SH": _frame(10.8, duplicate="conflict")})
    with pytest.raises(
        DuplicateConflictError,
        match=r"600001\.SH on 2025-01-03: fields=\['close', 'amount'\]",
    ):
        fetch_data_envelope(
            request,
            loaders={"primary": conflicting},
            capabilities={"primary": _capability("primary")},
        )


def test_dt07_capability_gate_does_not_call_provider_or_fake_adjustment() -> None:
    primary = _FakeLoader("primary", {"600001.SH": _frame(10.8)})
    request = _request(adjustment="qfq").model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )

    envelope = fetch_data_envelope(
        request,
        loaders={"primary": primary},
        capabilities={"primary": _capability("primary", adjustments=("raw",))},
    )

    assert primary.calls == []
    assert envelope.manifest.actual_sources == {"600001.SH": "not_available"}
    assert envelope.manifest.anomalies[0].kind == "unsupported_capability"
    with pytest.raises(IncompleteDataError, match="600001.SH"):
        envelope.require_complete()


def test_dt04_provider_errors_and_empty_results_remain_visible_when_all_fallbacks_fail() -> None:
    primary = _FakeLoader("primary", {}, errors={"600001.SH": TimeoutError("budget")})
    fallback = _FakeLoader("fallback", {})
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
        }
    )

    envelope = fetch_data_envelope(
        request,
        loaders={"primary": primary, "fallback": fallback},
        capabilities={"primary": _capability("primary"), "fallback": _capability("fallback")},
    )

    assert envelope.frames == {}
    assert envelope.manifest.actual_sources == {"600001.SH": "not_available"}
    assert [item.kind for item in envelope.manifest.anomalies] == [
        "provider_error",
        "empty_result",
    ]


def test_dt09_snapshot_hash_is_row_order_stable_and_invalidates_downstream_cache() -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    ordered = _FakeLoader("primary", {"600001.SH": _frame(10.8)})
    reversed_rows = _FakeLoader("primary", {"600001.SH": _frame(10.8).iloc[::-1]})
    changed = _FakeLoader("primary", {"600001.SH": _frame(10.9)})
    kwargs = {"capabilities": {"primary": _capability("primary")}}

    first = fetch_data_envelope(request, loaders={"primary": ordered}, **kwargs)
    second = fetch_data_envelope(request, loaders={"primary": reversed_rows}, **kwargs)
    third = fetch_data_envelope(request, loaders={"primary": changed}, **kwargs)

    assert first.manifest.snapshot_sha256 == second.manifest.snapshot_sha256
    assert first.manifest.snapshot_sha256 != third.manifest.snapshot_sha256
    first_cache = make_downstream_cache_key(
        snapshot_sha256=first.manifest.snapshot_sha256,
        consumer="quantaxis.adjust_prices",
        parameters={"anchor": "qfq-last"},
    )
    third_cache = make_downstream_cache_key(
        snapshot_sha256=third.manifest.snapshot_sha256,
        consumer="quantaxis.adjust_prices",
        parameters={"anchor": "qfq-last"},
    )
    assert first_cache != third_cache


def test_dt09_offline_snapshot_round_trip_never_constructs_or_calls_a_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    loader = _FakeLoader("primary", {"600001.SH": _frame(10.8)})
    envelope = fetch_data_envelope(
        request,
        loaders={"primary": loader},
        capabilities={"primary": _capability("primary")},
    ).require_complete()
    path = tmp_path / "snapshot.json"
    write_offline_snapshot(envelope, path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == envelope.manifest.snapshot_sha256

    def deny_network(*_args, **_kwargs):
        raise AssertionError("offline snapshot reader attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    replay = read_offline_snapshot(
        path,
        expected_sha256=envelope.manifest.snapshot_sha256,
    ).require_complete()

    assert loader.calls == ["600001.SH"]
    assert replay.manifest == envelope.manifest
    pd.testing.assert_frame_equal(replay.frames["600001.SH"], envelope.frames["600001.SH"])


def test_dt09_offline_snapshot_tamper_fails_content_validation(tmp_path: Path) -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    envelope = fetch_data_envelope(
        request,
        loaders={"primary": _FakeLoader("primary", {"600001.SH": _frame(10.8)})},
        capabilities={"primary": _capability("primary")},
    )
    path = tmp_path / "snapshot.json"
    write_offline_snapshot(envelope, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bars"]["600001.SH"][1]["close"] = 999.0
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="content does not match expected_sha256"):
        read_offline_snapshot(path, expected_sha256=envelope.manifest.snapshot_sha256)


def test_dt01_envelope_builds_public_snapshot_ref_with_same_content_identity() -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    envelope = fetch_data_envelope(
        request,
        loaders={"primary": _FakeLoader("primary", {"600001.SH": _frame(10.8)})},
        capabilities={"primary": _capability("primary")},
    )

    snapshot_ref = envelope.snapshot_ref()

    assert snapshot_ref.snapshot_sha256 == envelope.manifest.snapshot_sha256
    assert snapshot_ref.actual_sources == {"600001.SH": "primary"}
    assert snapshot_ref.fields == _FIELDS


def test_dt09_snapshot_reader_and_writer_reject_symlink_paths(tmp_path: Path) -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    envelope = fetch_data_envelope(
        request,
        loaders={"primary": _FakeLoader("primary", {"600001.SH": _frame(10.8)})},
        capabilities={"primary": _capability("primary")},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        write_offline_snapshot(envelope, linked_parent / "snapshot.json")

    real_snapshot = tmp_path / "snapshot.json"
    write_offline_snapshot(envelope, real_snapshot)
    linked_snapshot = tmp_path / "snapshot-link.json"
    linked_snapshot.symlink_to(real_snapshot)
    with pytest.raises(ValueError, match="regular, non-symlink"):
        read_offline_snapshot(
            linked_snapshot,
            expected_sha256=envelope.manifest.snapshot_sha256,
        )


def test_dt09_snapshot_write_is_idempotent_and_never_overwrites_history(tmp_path: Path) -> None:
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )
    first = fetch_data_envelope(
        request,
        loaders={"primary": _FakeLoader("primary", {"600001.SH": _frame(10.8)})},
        capabilities={"primary": _capability("primary")},
    )
    changed = fetch_data_envelope(
        request,
        loaders={"primary": _FakeLoader("primary", {"600001.SH": _frame(10.9)})},
        capabilities={"primary": _capability("primary")},
    )
    path = tmp_path / "snapshot.json"

    write_offline_snapshot(first, path)
    original_payload = path.read_bytes()
    original_inode = path.stat().st_ino
    write_offline_snapshot(first, path)

    assert path.read_bytes() == original_payload
    assert path.stat().st_ino == original_inode
    with pytest.raises(ValueError, match="existing offline snapshot has different content"):
        write_offline_snapshot(changed, path)
    assert path.read_bytes() == original_payload


def test_dt01_fallback_unit_conflict_within_instrument_type_fails_closed() -> None:
    primary = _FakeLoader("primary", {"600001.SH": _frame(10.8)})
    fallback = _FakeLoader("fallback", {"600002.SH": _frame(9.8)})
    fallback_units = dict(_STOCK_UNITS)
    fallback_units["volume"] = "lot_100_shares"
    fallback_capability = _capability("fallback").model_copy(
        update={
            "field_units": {
                "stock": fallback_units,
                "etf": fallback_units,
                "index": _INDEX_UNITS,
            }
        }
    )
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH", "600002.SH"),
            "instrument_types": {"600001.SH": "stock", "600002.SH": "stock"},
        }
    )

    with pytest.raises(UnitConflictError, match="stock.volume"):
        fetch_data_envelope(
            request,
            loaders={"primary": primary, "fallback": fallback},
            capabilities={
                "primary": _capability("primary"),
                "fallback": fallback_capability,
            },
        )


def test_dt03_invalid_ohlc_fails_at_envelope_boundary() -> None:
    invalid = _frame(10.8)
    invalid.loc[pd.Timestamp("2025-01-03"), "high"] = 10.0
    request = _request().model_copy(
        update={
            "symbols": ("600001.SH",),
            "instrument_types": {"600001.SH": "stock"},
            "requested_sources": ("primary",),
        }
    )

    with pytest.raises(ValueError, match="violates OHLC invariants on 2025-01-03"):
        fetch_data_envelope(
            request,
            loaders={"primary": _FakeLoader("primary", {"600001.SH": invalid})},
            capabilities={"primary": _capability("primary")},
        )
