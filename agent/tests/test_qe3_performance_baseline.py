"""QE3 PR-02 deterministic benchmark contract."""

from __future__ import annotations

from scripts.qe3_benchmark import run_baseline


def test_pr02_small_baseline_is_deterministic_and_bounded() -> None:
    first = run_baseline(sizes=(12,), repeats=1)
    second = run_baseline(sizes=(12,), repeats=1)

    assert first["schema_version"] == "vibe.qe3-performance-baseline.v1"
    assert first["cases"][0]["symbol_count"] == 12
    assert first["cases"][0]["candidate_count"] == 11
    assert (
        first["cases"][0]["result_sha256"]
        == second["cases"][0]["result_sha256"]
    )
    assert first["cases"][0]["median_ms"] < 5_000.0
    assert first["cases"][0]["peak_kib"] < 10_000.0
