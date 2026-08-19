"""Offline tests for the catalog-driven local canonical daily loader."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from backtest.loaders.base import NoAvailableSourceError
from backtest.loaders.local_canonical_loader import (
    DataLoader,
    LocalCanonicalDuplicateError,
    LocalCanonicalIncompleteError,
    LocalCanonicalIntegrityError,
    LocalCanonicalUnsupportedError,
    _canonical_version,
)
from backtest.loaders.registry import get_loader_cls_with_fallback
from src.market_data import fetch_market_data


OUTER_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = OUTER_ROOT / "tests" / "fixtures" / "canonical_daily_v1"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )


class TestLocalCanonicalLoader:
    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-canonical-loader-")
        self.root = Path(self.temp.name)
        self.canonical_root = self.root / "canonical"
        self.catalog_path = self.root / "catalog" / "a-share-daily-current.json"
        manifest = _read_json(FIXTURE / "manifest.json")
        version = manifest["canonical_version"]
        self.version_root = (
            self.canonical_root / "a-share-daily" / f"version={version}"
        )
        shutil.copytree(FIXTURE / "bars", self.version_root / "bars")
        shutil.copy2(FIXTURE / "manifest.json", self.version_root / "manifest.json")
        shutil.copy2(FIXTURE / "approval.json", self.version_root / "approval.json")
        self.catalog_path.parent.mkdir(parents=True)
        shutil.copy2(FIXTURE / "catalog.json", self.catalog_path)

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def loader(self) -> DataLoader:
        return DataLoader(
            catalog_path=self.catalog_path,
            canonical_root=self.canonical_root,
        )

    def test_reads_sh_sz_bj_and_cross_year_with_provenance(self) -> None:
        loader = self.loader()
        assert loader.is_available()
        result = loader.fetch(
            ["600000.SH", "000001.SZ", "920992.BJ"],
            "2025-12-30",
            "2026-01-05",
        )
        assert set(result) == {"600000.SH", "000001.SZ", "920992.BJ"}
        assert list(result["600000.SH"].index.strftime("%Y-%m-%d")) == [
            "2025-12-30",
            "2026-01-05",
        ]
        for frame in result.values():
            assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
            assert all(str(dtype) == "float64" for dtype in frame.dtypes)
            provenance = frame.attrs["provenance"]
            assert provenance["source"] == "local_canonical"
            assert provenance["provider"] == "fixture"
            assert provenance["canonical_version"] == _read_json(
                FIXTURE / "manifest.json"
            )["canonical_version"]
            assert provenance["adjustment"]["price_basis"] == "raw"
            assert provenance["watermark"] == "2026-01-05"
            assert provenance["completeness"] == "complete"
            assert provenance["fallback"] is False

    def test_registry_resolves_explicit_source_without_network_fallback(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("VIBE_LOCAL_CANONICAL_CATALOG", str(self.catalog_path))
        monkeypatch.setenv("VIBE_LOCAL_CANONICAL_ROOT", str(self.canonical_root))
        assert get_loader_cls_with_fallback("local_canonical") is DataLoader

        monkeypatch.setenv(
            "VIBE_LOCAL_CANONICAL_CATALOG", str(self.root / "missing-catalog.json")
        )
        with pytest.raises(NoAvailableSourceError, match="does not fall back"):
            get_loader_cls_with_fallback("local_canonical")

    def test_get_market_data_explicit_source_returns_rows_and_provenance(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("VIBE_LOCAL_CANONICAL_CATALOG", str(self.catalog_path))
        monkeypatch.setenv("VIBE_LOCAL_CANONICAL_ROOT", str(self.canonical_root))
        result = fetch_market_data(
            codes=["600000.SH"],
            start_date="2025-12-30",
            end_date="2026-01-05",
            source="local_canonical",
            max_rows=0,
        )
        assert [row["trade_date"] for row in result["600000.SH"]] == [
            "2025-12-30T00:00:00",
            "2026-01-05T00:00:00",
        ]
        provenance = result["_provenance"]["600000.SH"]
        assert provenance["source"] == "local_canonical"
        assert provenance["fallback"] is False

    def test_suspended_row_is_returned_and_volume_anomaly_is_excluded(self) -> None:
        result = self.loader().fetch(
            ["300176.SZ", "600000.SH"],
            "2025-12-31",
            "2025-12-31",
        )
        suspended = result["300176.SZ"].iloc[0]
        assert tuple(suspended[["open", "high", "low", "close", "volume"]]) == (
            5.23,
            5.23,
            5.23,
            5.23,
            0.0,
        )
        assert "600000.SH" not in result

    def test_unknown_but_well_formed_symbol_is_unresolved(self) -> None:
        assert self.loader().fetch(
            ["999999.SH"], "2025-12-30", "2025-12-30"
        ) == {}

    @pytest.mark.parametrize("interval", ["5m", "1H"])
    def test_rejects_non_daily_before_opening_catalog(self, interval: str) -> None:
        missing = DataLoader(
            catalog_path=self.root / "missing.json",
            canonical_root=self.root / "missing-root",
        )
        with pytest.raises(LocalCanonicalUnsupportedError, match="only interval=1D"):
            missing.fetch(["600000.SH"], "2025-01-01", "2025-01-02", interval=interval)

    def test_rejects_bad_symbol_and_excess_symbol_count_before_catalog(self) -> None:
        missing = DataLoader(
            catalog_path=self.root / "missing.json",
            canonical_root=self.root / "missing-root",
        )
        with pytest.raises(LocalCanonicalUnsupportedError, match="unsupported"):
            missing.fetch(["600000.SH') OR TRUE --"], "2025-01-01", "2025-01-02")
        codes = [f"{number:06}.SH" for number in range(129)]
        with pytest.raises(ValueError, match="1..128"):
            missing.fetch(codes, "2025-01-01", "2025-01-02")

    @pytest.mark.parametrize(
        "start,end",
        [("2025-12-29", "2025-12-30"), ("2026-01-05", "2026-01-06")],
    )
    def test_outside_watermark_fails_incomplete(self, start: str, end: str) -> None:
        with pytest.raises(LocalCanonicalIncompleteError, match="outside canonical coverage"):
            self.loader().fetch(["600000.SH"], start, end)

    def test_catalog_path_mismatch_fails_closed(self) -> None:
        catalog = _read_json(self.catalog_path)
        catalog["manifest_path"] = "../manifest.json"
        _write_json(self.catalog_path, catalog)
        loader = self.loader()
        assert not loader.is_available()
        with pytest.raises(LocalCanonicalIntegrityError, match="manifest path"):
            loader.fetch(["600000.SH"], "2025-12-30", "2025-12-30")

    def test_file_size_drift_fails_closed(self) -> None:
        path = self.version_root / "bars" / "year=2025" / "stock_daily.parquet"
        path.write_bytes(path.read_bytes()[:-1])
        loader = self.loader()
        assert not loader.is_available()
        with pytest.raises(LocalCanonicalIntegrityError, match="size"):
            loader.fetch(["600000.SH"], "2025-12-30", "2025-12-30")

    def test_duplicate_natural_key_fails_instead_of_selecting_a_row(self) -> None:
        manifest = _read_json(self.version_root / "manifest.json")
        target = self.version_root / "bars" / "year=2026" / "stock_daily.parquet"
        shutil.copy2(FIXTURE / "adversarial" / "duplicate_conflict.parquet", target)
        entry = next(item for item in manifest["files"] if item["year"] == 2026)
        entry.update(
            {
                "size_bytes": target.stat().st_size,
                "row_count": 2,
                "symbol_count": 1,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "min_trade_date": "2026-01-05",
                "max_trade_date": "2026-01-05",
            }
        )
        new_version = _canonical_version(manifest)
        manifest["canonical_version"] = new_version
        old_root = self.version_root
        self.version_root = old_root.with_name(f"version={new_version}")
        old_root.rename(self.version_root)
        _write_json(self.version_root / "manifest.json", manifest)
        catalog = _read_json(self.catalog_path)
        catalog["current_version"] = new_version
        catalog["manifest_path"] = (
            f"a-share-daily/version={new_version}/manifest.json"
        )
        _write_json(self.catalog_path, catalog)

        with pytest.raises(LocalCanonicalDuplicateError, match="600000.SH"):
            self.loader().fetch(["600000.SH"], "2026-01-05", "2026-01-05")
