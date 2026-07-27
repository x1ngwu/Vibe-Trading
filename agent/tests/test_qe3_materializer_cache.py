"""QE3 production materializer source-cache regressions."""

from __future__ import annotations

import stat

import pandas as pd

from scripts.qe3_materialize_production import _cached_source_frame


def test_successful_upstream_table_is_persisted_and_reused(tmp_path) -> None:
    cache_path = tmp_path / "source-cache" / "upstream.json"
    calls = 0

    def fetch() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260724",
                    "total_mv": 123.45,
                }
            ]
        )

    first = _cached_source_frame(cache_path, fetch)
    second = _cached_source_frame(
        cache_path,
        lambda: (_ for _ in ()).throw(AssertionError("unexpected upstream call")),
    )

    assert calls == 1
    pd.testing.assert_frame_equal(second, first)
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
