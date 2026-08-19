"""Contract tests for the local canonical benchmark artifact."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from scripts.local_canonical_benchmark import (
    BenchmarkCase,
    load_symbols,
    run_benchmark,
    run_case,
)


OUTER_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = OUTER_ROOT / "tests" / "fixtures" / "canonical_daily_v1"


def test_fixture_benchmark_reports_version_rows_and_memory() -> None:
    with tempfile.TemporaryDirectory(prefix="local-canonical-benchmark-") as raw:
        root = Path(raw)
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        version = manifest["canonical_version"]
        version_root = root / "canonical" / "a-share-daily" / f"version={version}"
        shutil.copytree(FIXTURE / "bars", version_root / "bars")
        shutil.copy2(FIXTURE / "manifest.json", version_root / "manifest.json")
        catalog = root / "catalog" / "a-share-daily-current.json"
        catalog.parent.mkdir()
        shutil.copy2(FIXTURE / "catalog.json", catalog)

        report = run_benchmark(
            catalog_path=catalog,
            canonical_root=root / "canonical",
            symbols=["600000.SH", "000001.SZ", "920992.BJ"],
            cases=(BenchmarkCase("fixture_three_symbols", 3, 2, 30.0),),
        )

        assert report["schema_version"] == "vibe.local-canonical-benchmark.v1"
        assert report["dataset"]["canonical_version"] == version
        assert report["method"]["network_required"] is False
        assert report["method"]["os_page_cache"] == "uncontrolled"
        case = report["cases"][0]
        assert case["request"]["chunk_count"] == 1
        assert case["result"]["resolved_symbol_count"] == 3
        assert case["result"]["unresolved_symbol_count"] == 0
        assert case["result"]["row_count"] == 6
        assert case["result"]["canonical_versions"] == [version]
        assert case["measurements"]["peak_rss_kib"] is not None
        assert report["passed"] is True


def test_symbols_file_requires_unique_nonempty_values(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text('["600000.sh", "000001.SZ"]\n', encoding="utf-8")
    assert load_symbols(path) == ["600000.SH", "000001.SZ"]

    path.write_text('["600000.SH", "600000.sh"]\n', encoding="utf-8")
    try:
        load_symbols(path)
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate symbols must be rejected")


def test_five_hundred_symbol_case_uses_four_bounded_chunks() -> None:
    version = "a" * 64

    class Frame:
        attrs = {"provenance": {"canonical_version": version}}

        def __len__(self) -> int:
            return 1

    class Loader:
        def __init__(self) -> None:
            self.chunk_sizes: list[int] = []

        def _dataset(self):
            return SimpleNamespace(
                minimum=date(2021, 1, 1),
                maximum=date(2026, 8, 14),
                manifest={"canonical_version": version},
            )

        def fetch(self, symbols, start_date, end_date):
            self.chunk_sizes.append(len(symbols))
            return {symbol: Frame() for symbol in symbols}

    loader = Loader()
    symbols = [f"{index:06d}.SH" for index in range(500)]
    result = run_case(
        loader,  # type: ignore[arg-type]
        symbols,
        BenchmarkCase("five_hundred", 500, 3, 60.0),
    )
    assert loader.chunk_sizes == [128, 128, 128, 116]
    assert result["request"]["chunk_count"] == 4
    assert result["result"]["resolved_symbol_count"] == 500
    assert result["result"]["row_count"] == 500
    assert result["passed"] is True
