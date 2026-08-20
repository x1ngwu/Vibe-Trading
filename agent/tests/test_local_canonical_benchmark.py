"""Contract tests for the local canonical benchmark artifact."""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backtest.loaders.local_canonical_loader import DataLoader
from scripts.local_canonical_benchmark import (
    BenchmarkCase,
    load_symbols,
    prepare_page_cache,
    run_benchmark,
    run_case,
    validate_cache_preparation,
)


OUTER_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = OUTER_ROOT / "tests" / "fixtures" / "canonical_daily_v1"
FIXED_NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
RESTORE_ID = "restore-20260820-000000Z-01234567"
SNAPSHOT_ID = "a" * 64
ORACLE_ID = "oracle-20260820-000000Z"


def _copy_fixture(root: Path) -> tuple[Path, Path]:
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["canonical_version"]
    canonical_root = root / "canonical"
    version_root = canonical_root / "a-share-daily" / f"version={version}"
    shutil.copytree(FIXTURE / "bars", version_root / "bars")
    shutil.copy2(FIXTURE / "manifest.json", version_root / "manifest.json")
    catalog = root / "catalog" / "a-share-daily-current.json"
    catalog.parent.mkdir()
    shutil.copy2(FIXTURE / "catalog.json", catalog)
    return catalog, canonical_root


def _prepare_fixture(
    catalog: Path,
    canonical_root: Path,
    *,
    advisor,
) -> dict:
    dataset = DataLoader(catalog_path=catalog, canonical_root=canonical_root)._dataset()
    return prepare_page_cache(
        dataset,
        restore_id=RESTORE_ID,
        snapshot_id=SNAPSHOT_ID,
        oracle_id=ORACLE_ID,
        advisor=advisor,
        advice=4,
        now=FIXED_NOW,
    )


def test_fixture_benchmark_reports_version_rows_and_memory() -> None:
    with tempfile.TemporaryDirectory(prefix="local-canonical-benchmark-") as raw:
        root = Path(raw)
        catalog, canonical_root = _copy_fixture(root)
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        version = manifest["canonical_version"]

        report = run_benchmark(
            catalog_path=catalog,
            canonical_root=canonical_root,
            symbols=["600000.SH", "000001.SZ", "920992.BJ"],
            cases=(BenchmarkCase("fixture_three_symbols", 3, 2, 30.0),),
        )

        assert report["schema_version"] == "vibe.local-canonical-benchmark.v1"
        assert report["dataset"]["canonical_version"] == version
        assert report["method"]["network_required"] is False
        assert report["method"]["os_page_cache"] == "uncontrolled"
        assert report["method"]["cold_cache"] is False
        assert report["method"]["cache_preparation"] == {
            "requested": False,
            "status": "not_requested",
            "validated": False,
        }
        case = report["cases"][0]
        assert case["request"]["chunk_count"] == 1
        assert case["result"]["resolved_symbol_count"] == 3
        assert case["result"]["unresolved_symbol_count"] == 0
        assert case["result"]["row_count"] == 6
        assert case["result"]["canonical_versions"] == [version]
        assert case["measurements"]["peak_rss_kib"] is not None
        assert report["passed"] is True


def test_cache_preparation_fails_closed_when_platform_is_unsupported(
    tmp_path: Path,
) -> None:
    catalog, canonical_root = _copy_fixture(tmp_path)
    dataset = DataLoader(catalog_path=catalog, canonical_root=canonical_root)._dataset()

    report = prepare_page_cache(
        dataset,
        restore_id=RESTORE_ID,
        snapshot_id=SNAPSHOT_ID,
        oracle_id=ORACLE_ID,
        advisor=None,
        advice=None,
        now=FIXED_NOW,
    )

    assert report["result"] == {
        "status": "unsupported",
        "supported": False,
        "file_count": 2,
        "succeeded_file_count": 0,
        "failed_file_count": 2,
    }
    assert [item["status"] for item in report["files"]] == [
        "not_attempted",
        "not_attempted",
    ]
    assert str(tmp_path) not in json.dumps(report)
    validation = validate_cache_preparation(dataset, report, now=FIXED_NOW)
    assert validation["validated"] is False
    assert validation["validation_error"] == "preparation_not_succeeded"


def test_partial_cache_preparation_never_marks_benchmark_cold(tmp_path: Path) -> None:
    catalog, canonical_root = _copy_fixture(tmp_path)
    calls = 0

    def partial_advisor(descriptor: int, offset: int, length: int, advice: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "fixture failure")

    preparation = _prepare_fixture(
        catalog,
        canonical_root,
        advisor=partial_advisor,
    )
    assert preparation["result"]["status"] == "failed"
    assert preparation["result"]["succeeded_file_count"] == 1
    assert preparation["result"]["failed_file_count"] == 1
    assert [item["status"] for item in preparation["files"]] == [
        "succeeded",
        "failed",
    ]
    assert preparation["files"][1]["error"] == "OSError"
    assert preparation["files"][1]["errno"] == errno.EIO
    assert str(tmp_path) not in json.dumps(preparation)

    benchmark = run_benchmark(
        catalog_path=catalog,
        canonical_root=canonical_root,
        symbols=["600000.SH", "000001.SZ", "920992.BJ"],
        cases=(BenchmarkCase("fixture_three_symbols", 3, 2, 30.0),),
        cache_preparation=preparation,
        now=FIXED_NOW,
    )
    assert benchmark["method"]["os_page_cache"] == "uncontrolled"
    assert benchmark["method"]["cold_cache"] is False
    assert benchmark["method"]["cache_preparation"]["validated"] is False
    assert benchmark["passed"] is False


def test_complete_cache_preparation_is_validated_as_cold(tmp_path: Path) -> None:
    catalog, canonical_root = _copy_fixture(tmp_path)
    advised_sizes: list[int] = []

    def successful_advisor(
        descriptor: int,
        offset: int,
        length: int,
        advice: int,
    ) -> None:
        assert offset == 0
        assert length == 0
        assert advice == 4
        advised_sizes.append(os.fstat(descriptor).st_size)

    preparation = _prepare_fixture(
        catalog,
        canonical_root,
        advisor=successful_advisor,
    )
    assert preparation["result"]["status"] == "succeeded"
    assert preparation["result"]["succeeded_file_count"] == 2
    assert preparation["result"]["failed_file_count"] == 0
    assert advised_sizes == [2431, 2047]
    assert all(item["status"] == "succeeded" for item in preparation["files"])
    assert str(tmp_path) not in json.dumps(preparation)

    benchmark = run_benchmark(
        catalog_path=catalog,
        canonical_root=canonical_root,
        symbols=["600000.SH", "000001.SZ", "920992.BJ"],
        cases=(BenchmarkCase("fixture_three_symbols", 3, 2, 30.0),),
        cache_preparation=preparation,
        now=FIXED_NOW,
    )
    assert benchmark["method"]["os_page_cache"] == (
        "per_file_posix_fadvise_dontneed"
    )
    assert benchmark["method"]["cold_cache"] is True
    assert benchmark["method"]["cache_preparation"]["validated"] is True
    assert benchmark["method"]["cache_preparation"]["recovery_evidence"] == {
        "restore_id": RESTORE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "oracle_id": ORACLE_ID,
    }
    assert benchmark["passed"] is True


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
