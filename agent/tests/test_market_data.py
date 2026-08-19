"""Tests for the shared market-data helper layer.

``src.market_data`` is the source-resolution + normalization layer shared by
the MCP server and the agent ``get_market_data`` tool. It shipped (with the
#270 global data layer) without dedicated tests. These cover the
network-free logic: source detection, row capping, JSON-safety, and the
``fetch_market_data`` orchestration via an injected stub loader.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.market_data import (
    DEFAULT_MAX_ROWS,
    _json_safe,
    cap_rows,
    detect_source,
    fetch_market_data,
    fetch_market_data_json,
    local_canonical_mode,
)


# --------------------------------------------------------------------------
# detect_source
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("600519.SH", "tencent"),
        ("000001.SZ", "tencent"),
        ("430139.BJ", "tencent"),
        ("AAPL.US", "yahoo"),
        ("^GSPC", "yahoo"),
        ("^IXIC", "yahoo"),
        ("^DJI", "yahoo"),
        ("700.HK", "yahoo"),
        ("00700.HK", "yahoo"),
        ("RELIANCE.NS", "yahoo"),  # India NSE
        ("TCS.NS", "yahoo"),
        ("M&M.NS", "yahoo"),  # ampersand in ticker
        ("BAJAJ-AUTO.NS", "yahoo"),  # hyphen in ticker
        ("500325.BO", "yahoo"),  # India BSE (numeric scrip code)
        ("BTC-USDT", "okx"),
        ("ETH/USDT", "ccxt"),
        ("local:my_file", "local"),
        ("something_weird", "tushare"),  # documented fallback
    ],
)
def test_detect_source(code: str, expected: str) -> None:
    assert detect_source(code) == expected


# --------------------------------------------------------------------------
# cap_rows
# --------------------------------------------------------------------------


def test_cap_rows_passthrough_when_under_limit() -> None:
    rows = [{"a": i} for i in range(3)]
    assert cap_rows(rows, 250) is rows


def test_cap_rows_zero_means_no_cap() -> None:
    rows = [{"a": i} for i in range(1000)]
    assert cap_rows(rows, 0) is rows


def test_cap_rows_negative_falls_back_to_default() -> None:
    rows = [{"a": i} for i in range(DEFAULT_MAX_ROWS + 10)]
    out = cap_rows(rows, -5)
    # Negative max_rows is treated as DEFAULT_MAX_ROWS -> truncated payload.
    assert isinstance(out, dict)
    assert out["truncated"] is True


def test_cap_rows_samples_with_stride_and_pins_last() -> None:
    rows = [{"a": i} for i in range(10)]
    out = cap_rows(rows, 4)
    assert isinstance(out, dict)
    assert out["rows"] == 10
    assert out["truncated"] is True
    # Even stride of ceil(10/4)=3 plus the pinned final bar.
    assert out["data"][0] == {"a": 0}
    assert out["data"][-1] == {"a": 9}  # last bar always pinned
    assert out["returned"] == len(out["data"])


# --------------------------------------------------------------------------
# _json_safe
# --------------------------------------------------------------------------


def test_json_safe_non_finite_becomes_none() -> None:
    assert _json_safe(float("nan")) is None
    assert _json_safe(float("inf")) is None
    assert _json_safe(float("-inf")) is None


def test_json_safe_timestamp_isoformat() -> None:
    assert _json_safe(pd.Timestamp("2026-01-01")) == "2026-01-01T00:00:00"


def test_json_safe_numpy_scalar_unwrapped() -> None:
    out = _json_safe(np.int64(5))
    assert out == 5
    assert not isinstance(out, np.integer)


def test_json_safe_plain_value_passthrough() -> None:
    assert _json_safe("hello") == "hello"
    assert _json_safe(3.5) == 3.5


# --------------------------------------------------------------------------
# fetch_market_data (stub loader — no network)
# --------------------------------------------------------------------------


class _StubLoader:
    """Returns a fixed 2-row OHLCV frame for every requested code."""

    def __init__(self) -> None:
        pass

    def fetch(self, codes, start_date, end_date, interval="1D"):
        idx = pd.to_datetime(["2026-01-01", "2026-01-02"])
        idx.name = "trade_date"
        return {
            code: pd.DataFrame({"close": [1.0, 2.0], "volume": [100, 200]}, index=idx)
            for code in codes
        }


class _BadLoader:
    def __init__(self) -> None:
        pass

    def fetch(self, *args, **kwargs):
        raise RuntimeError("loader exploded")


class _PartialLoader:
    """Returns data for only the first requested code."""

    def __init__(self) -> None:
        pass

    def fetch(self, codes, start_date, end_date, interval="1D"):
        idx = pd.to_datetime(["2026-01-01"])
        idx.name = "trade_date"
        return {codes[0]: pd.DataFrame({"close": [1.0]}, index=idx)}


class _ProvenanceLoader:
    def fetch(self, codes, start_date, end_date, interval="1D"):
        idx = pd.to_datetime(["2026-01-01"])
        idx.name = "trade_date"
        frame = pd.DataFrame({"close": [1.0]}, index=idx)
        frame.attrs["provenance"] = {
            "source": "local_canonical",
            "canonical_version": "a" * 64,
            "fallback": False,
        }
        return {codes[0]: frame}


class _IncompleteLocalLoader:
    def fetch(self, *args, **kwargs):
        error = RuntimeError("requested range is outside canonical coverage")
        error.status = "incomplete"
        raise error


class _IntegrityLocalLoader:
    name = "local_canonical"

    def fetch(self, *args, **kwargs):
        error = RuntimeError("canonical manifest identity is invalid")
        error.status = "integrity_error"
        raise error


def test_fetch_explicit_source_normalizes_rows() -> None:
    out = fetch_market_data(
        codes=["AAPL.US"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="yahoo",
        loader_resolver=lambda src: _StubLoader,
    )
    assert "AAPL.US" in out
    rows = out["AAPL.US"]
    assert rows[0]["trade_date"] == "2026-01-01T00:00:00"  # index reset + isoformat
    assert rows[0]["close"] == 1.0


def test_fetch_auto_groups_by_detected_source() -> None:
    seen: dict[str, list[str]] = {}

    def resolver(src: str):
        seen[src] = []
        return _StubLoader

    out = fetch_market_data(
        codes=["AAPL.US", "BTC-USDT"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    # AAPL.US -> yahoo, BTC-USDT -> okx: two distinct loader groups resolved.
    assert set(seen) == {"yahoo", "okx"}
    assert "AAPL.US" in out and "BTC-USDT" in out


@pytest.mark.parametrize("mode", ["disabled", "explicit"])
def test_auto_a_share_preserves_network_route_until_auto_mode(
    monkeypatch, mode: str
) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", mode)
    seen: list[str] = []

    def resolver(src: str):
        seen.append(src)
        return _StubLoader

    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    assert "600519.SH" in out
    assert seen == ["tencent"]
    assert out["_routing"]["600519.SH"]["actual_source"] == "tencent"


def test_disabled_mode_rejects_explicit_local_without_resolving_loader(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "disabled")

    def unexpected_resolver(_src: str):
        raise AssertionError("disabled mode must not resolve or open the catalog")

    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="local_canonical",
        loader_resolver=unexpected_resolver,
    )
    assert out["_unresolved"] == ["600519.SH"]
    assert out["_errors"]["local_canonical"]["status"] == "source_disabled"


def test_auto_mode_uses_local_for_covered_a_share_daily(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "auto")
    seen: list[str] = []

    def resolver(src: str):
        seen.append(src)
        return _StubLoader

    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    assert seen == ["local_canonical"]
    assert out["_routing"]["600519.SH"] == {
        "requested_source": "auto",
        "preferred_source": "local_canonical",
        "actual_source": "local_canonical",
        "fallback": False,
    }


def test_auto_mode_incomplete_local_uses_one_network_source_with_reason(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "auto")
    seen: list[str] = []

    def resolver(src: str):
        seen.append(src)
        return _IncompleteLocalLoader if src == "local_canonical" else _StubLoader

    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2025-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    assert seen == ["local_canonical", "tencent"]
    assert "600519.SH" in out
    assert out["_routing"]["600519.SH"]["fallback"] is True
    assert (
        out["_routing"]["600519.SH"]["fallback_reason"]
        == "local_coverage_incomplete"
    )


def test_auto_mode_falls_back_only_missing_symbol_without_cross_source_seam(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "auto")
    network_codes: list[str] = []

    class PartialLocal:
        name = "local_canonical"

        def fetch(self, codes, start_date, end_date, interval="1D"):
            idx = pd.to_datetime(["2026-01-01"])
            idx.name = "trade_date"
            return {
                codes[0]: pd.DataFrame({"close": [1.0], "volume": [100]}, index=idx)
            }

    class RecordingNetwork(_StubLoader):
        name = "tencent"

        def fetch(self, codes, start_date, end_date, interval="1D"):
            network_codes.extend(codes)
            return super().fetch(codes, start_date, end_date, interval=interval)

    def resolver(src: str):
        return PartialLocal if src == "local_canonical" else RecordingNetwork

    out = fetch_market_data(
        codes=["600519.SH", "000001.SZ"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    assert network_codes == ["000001.SZ"]
    assert out["_routing"]["600519.SH"]["actual_source"] == "local_canonical"
    assert out["_routing"]["000001.SZ"]["actual_source"] == "tencent"
    assert out["_routing"]["000001.SZ"]["fallback_reason"] == "local_no_data"


def test_auto_mode_integrity_error_fails_closed_without_network(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "auto")
    seen: list[str] = []

    def resolver(src: str):
        seen.append(src)
        return _IntegrityLocalLoader

    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=resolver,
    )
    assert seen == ["local_canonical"]
    assert out["_unresolved"] == ["600519.SH"]
    assert out["_errors"]["local_canonical"]["status"] == "integrity_error"


def test_auto_mode_never_uses_daily_local_for_intraday(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "auto")
    seen: list[str] = []

    def resolver(src: str):
        seen.append(src)
        return _StubLoader

    fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        interval="5m",
        loader_resolver=resolver,
    )
    assert seen == ["tencent"]


def test_invalid_local_mode_fails_closed_before_loader_resolution(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "sometimes")
    with pytest.raises(ValueError, match="must be one of"):
        local_canonical_mode()
    out = fetch_market_data(
        codes=["600519.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="auto",
        loader_resolver=lambda _src: pytest.fail("loader must not be resolved"),
    )
    assert out["_errors"]["local_canonical"]["status"] == "invalid_configuration"


def test_fetch_loader_error_falls_through_to_unresolved() -> None:
    out = fetch_market_data(
        codes=["X.US"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="yahoo",
        loader_resolver=lambda src: _BadLoader,
    )
    assert out["_unresolved"] == ["X.US"]


def test_fetch_missing_symbol_listed_as_unresolved() -> None:
    out = fetch_market_data(
        codes=["A.US", "B.US"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="yahoo",
        loader_resolver=lambda src: _PartialLoader,
    )
    assert "A.US" in out
    assert out["_unresolved"] == ["B.US"]


def test_fetch_preserves_frame_provenance(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "explicit")
    out = fetch_market_data(
        codes=["600000.SH"],
        start_date="2026-01-01",
        end_date="2026-01-01",
        source="local_canonical",
        loader_resolver=lambda src: _ProvenanceLoader,
    )
    assert out["_provenance"]["600000.SH"] == {
        "source": "local_canonical",
        "canonical_version": "a" * 64,
        "fallback": False,
    }


def test_explicit_local_canonical_error_is_visible_and_unresolved(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_LOCAL_CANONICAL_MODE", "explicit")
    out = fetch_market_data(
        codes=["600000.SH"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="local_canonical",
        loader_resolver=lambda src: _IncompleteLocalLoader,
    )
    assert out["_unresolved"] == ["600000.SH"]
    assert out["_errors"]["local_canonical"] == {
        "status": "incomplete",
        "detail": "requested range is outside canonical coverage",
    }


# --------------------------------------------------------------------------
# fetch_market_data_json
# --------------------------------------------------------------------------


def test_fetch_json_is_strict_and_parseable() -> None:
    payload = fetch_market_data_json(
        codes=["AAPL.US"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="yahoo",
        loader_resolver=lambda src: _StubLoader,
    )
    parsed = json.loads(payload)  # must be valid JSON
    assert "AAPL.US" in parsed


def test_fetch_json_rejects_nan_via_allow_nan_false() -> None:
    class _NanLoader:
        def __init__(self) -> None:
            pass

        def fetch(self, codes, start_date, end_date, interval="1D"):
            idx = pd.to_datetime(["2026-01-01"])
            idx.name = "trade_date"
            # A NaN close must be sanitized to null by _json_safe, so strict
            # JSON (allow_nan=False) still succeeds.
            return {codes[0]: pd.DataFrame({"close": [float("nan")]}, index=idx)}

    payload = fetch_market_data_json(
        codes=["A.US"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="yahoo",
        loader_resolver=lambda src: _NanLoader,
    )
    parsed = json.loads(payload)
    assert parsed["A.US"][0]["close"] is None
