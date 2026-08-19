"""Read-only loader for the content-addressed local A-share daily dataset."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional

import duckdb
import pandas as pd

from backtest.loaders.registry import register


CATALOG_SCHEMA = "vibe.market-data-catalog.v1"
MANIFEST_SCHEMA = "vibe.canonical-daily-manifest.v1"
DEFAULT_CATALOG = Path(
    "/var/lib/vibe-trading/market-data/catalog/a-share-daily-current.json"
)
DEFAULT_CANONICAL_ROOT = Path("/var/lib/vibe-trading/market-data/canonical")
MAX_SYMBOLS = 128
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONAL_MANIFEST_FIELDS = {
    "canonical_version",
    "created_at",
    "published_at",
    "operator_display_name",
}


class LocalCanonicalError(RuntimeError):
    """Base class for stable local-canonical request failures."""

    status = "source_unavailable"


class LocalCanonicalIntegrityError(LocalCanonicalError):
    status = "integrity_error"


class LocalCanonicalUnsupportedError(LocalCanonicalError):
    status = "unsupported_capability"


class LocalCanonicalIncompleteError(LocalCanonicalError):
    status = "incomplete"


class LocalCanonicalDuplicateError(LocalCanonicalIntegrityError):
    status = "duplicate_conflict"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_version(manifest: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in _OPERATIONAL_MANIFEST_FIELDS
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _read_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise LocalCanonicalIntegrityError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise LocalCanonicalIntegrityError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalCanonicalIntegrityError(f"{label} is not readable valid JSON") from exc
    if not isinstance(value, dict):
        raise LocalCanonicalIntegrityError(f"{label} must contain a JSON object")
    return value


def _validate_root(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LocalCanonicalIntegrityError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise LocalCanonicalIntegrityError(f"{label} must be a real directory")
    return resolved


def _safe_relative_file(root: Path, relative_text: str, label: str) -> Path:
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise LocalCanonicalIntegrityError(f"{label} path is not normalized and relative")
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise LocalCanonicalIntegrityError(f"{label} path component is unavailable") from exc
        if current.is_symlink():
            raise LocalCanonicalIntegrityError(f"{label} path contains a symlink")
        if current != candidate and not stat.S_ISDIR(mode):
            raise LocalCanonicalIntegrityError(f"{label} parent is not a directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LocalCanonicalIntegrityError(f"{label} is unavailable") from exc
    if root not in resolved.parents:
        raise LocalCanonicalIntegrityError(f"{label} escapes the canonical root")
    if not stat.S_ISREG(candidate.lstat().st_mode):
        raise LocalCanonicalIntegrityError(f"{label} must be a regular file")
    return resolved


def _strict_date(value: str, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid calendar date") from exc


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalCanonicalIntegrityError(f"manifest {field} must be an object")
    return value


class _Dataset:
    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        version_root: Path,
        files_by_year: dict[int, Path],
    ) -> None:
        self.manifest = manifest
        self.version_root = version_root
        self.files_by_year = files_by_year
        self.minimum = _strict_date(manifest["coverage"]["min_trade_date"], "min_trade_date")
        self.maximum = _strict_date(manifest["coverage"]["max_trade_date"], "max_trade_date")

    @property
    def provenance(self) -> dict[str, Any]:
        provider = self.manifest["provider"]
        source = self.manifest["source"]
        return {
            "source": "local_canonical",
            "provider": provider["provider_id"],
            "provider_version": provider["provider_version"],
            "canonical_version": self.manifest["canonical_version"],
            "source_batch_id": source["batch_id"],
            "adjustment": deepcopy(self.manifest["adjustment"]),
            "units": deepcopy(self.manifest["units"]),
            "watermark": self.manifest["coverage"]["as_of"],
            "completeness": "complete",
            "fallback": False,
            "warnings": list(self.manifest["quality"]["warnings"]),
        }


def _load_dataset(catalog_path: Path, canonical_root: Path) -> _Dataset:
    catalog = _read_json_file(catalog_path, "local canonical catalog")
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise LocalCanonicalIntegrityError("catalog schema_version is unsupported")
    if catalog.get("dataset") != "a-share-daily" or catalog.get("required") is not True:
        raise LocalCanonicalIntegrityError("catalog dataset contract is invalid")
    version = catalog.get("current_version")
    if not isinstance(version, str) or not _SHA256_RE.fullmatch(version):
        raise LocalCanonicalIntegrityError("catalog current_version is invalid")
    expected_manifest = f"a-share-daily/version={version}/manifest.json"
    if catalog.get("manifest_path") != expected_manifest:
        raise LocalCanonicalIntegrityError("catalog manifest path does not match current_version")

    root = _validate_root(canonical_root, "local canonical root")
    manifest_path = _safe_relative_file(root, expected_manifest, "canonical manifest")
    manifest = _read_json_file(manifest_path, "canonical manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise LocalCanonicalIntegrityError("manifest schema_version is unsupported")
    if manifest.get("dataset") != "a-share-daily":
        raise LocalCanonicalIntegrityError("manifest dataset is invalid")
    if manifest.get("canonical_version") != version or _canonical_version(manifest) != version:
        raise LocalCanonicalIntegrityError("manifest canonical identity does not match catalog")

    scope = _require_mapping(manifest.get("scope"), "scope")
    if (
        scope.get("instrument_types") != ["equity"]
        or scope.get("markets") != ["SH", "SZ", "BJ"]
        or scope.get("frequency") != "1D"
        or scope.get("timezone") != "Asia/Shanghai"
    ):
        raise LocalCanonicalIntegrityError("manifest scope exceeds local daily capability")
    capabilities = _require_mapping(manifest.get("capabilities"), "capabilities")
    if capabilities.get("supported_intervals") != ["1D"]:
        raise LocalCanonicalIntegrityError("manifest interval capability is invalid")
    if _require_mapping(capabilities.get("price_query"), "price_query").get("status") != "enabled":
        raise LocalCanonicalIntegrityError("manifest does not enable price queries")
    if _require_mapping(capabilities.get("chart"), "chart").get("status") != "enabled":
        raise LocalCanonicalIntegrityError("manifest does not enable charts")
    for blocked in ("backtest", "execution"):
        if _require_mapping(capabilities.get(blocked), blocked).get("status") != "blocked":
            raise LocalCanonicalIntegrityError(f"manifest must block {blocked}")

    version_root = manifest_path.parent
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise LocalCanonicalIntegrityError("manifest file inventory is empty")
    files_by_year: dict[int, Path] = {}
    for entry in raw_files:
        if not isinstance(entry, Mapping):
            raise LocalCanonicalIntegrityError("manifest file entry is invalid")
        year = entry.get("year")
        relative = entry.get("path")
        if not isinstance(year, int) or year in files_by_year or not isinstance(relative, str):
            raise LocalCanonicalIntegrityError("manifest file year/path is invalid")
        expected = f"bars/year={year}/stock_daily.parquet"
        if relative != expected:
            raise LocalCanonicalIntegrityError("manifest file path is not canonical")
        path = _safe_relative_file(version_root, relative, f"canonical bars for {year}")
        size = entry.get("size_bytes")
        if not isinstance(size, int) or path.stat().st_size != size:
            raise LocalCanonicalIntegrityError("canonical file size does not match manifest")
        files_by_year[year] = path

    coverage = _require_mapping(manifest.get("coverage"), "coverage")
    quality = _require_mapping(manifest.get("quality"), "quality")
    provider = _require_mapping(manifest.get("provider"), "provider")
    source = _require_mapping(manifest.get("source"), "source")
    adjustment = _require_mapping(manifest.get("adjustment"), "adjustment")
    units = _require_mapping(manifest.get("units"), "units")
    for name, value in (
        ("provider_id", provider.get("provider_id")),
        ("provider_version", provider.get("provider_version")),
        ("batch_id", source.get("batch_id")),
        ("price_basis", adjustment.get("price_basis")),
        ("as_of", coverage.get("as_of")),
    ):
        if not isinstance(value, str) or not value:
            raise LocalCanonicalIntegrityError(f"manifest {name} is missing")
    if not isinstance(quality.get("warnings"), list):
        raise LocalCanonicalIntegrityError("manifest warnings are invalid")
    for field in ("open", "high", "low", "close", "vol", "amount"):
        unit = _require_mapping(units.get(field), f"units.{field}")
        if not isinstance(unit.get("value"), str):
            raise LocalCanonicalIntegrityError(f"manifest unit {field} is invalid")
    return _Dataset(manifest=manifest, version_root=version_root, files_by_year=files_by_year)


@register
class DataLoader:
    """Catalog-driven, read-only A-share daily loader."""

    name = "local_canonical"
    markets = {"a_share"}
    requires_auth = False

    def __init__(
        self,
        *,
        catalog_path: Path | None = None,
        canonical_root: Path | None = None,
    ) -> None:
        self._catalog_path = catalog_path or Path(
            os.getenv("VIBE_LOCAL_CANONICAL_CATALOG", str(DEFAULT_CATALOG))
        )
        self._canonical_root = canonical_root or Path(
            os.getenv("VIBE_LOCAL_CANONICAL_ROOT", str(DEFAULT_CANONICAL_ROOT))
        )

    def _dataset(self) -> _Dataset:
        return _load_dataset(self._catalog_path, self._canonical_root)

    def is_available(self) -> bool:
        try:
            self._dataset()
        except (LocalCanonicalError, ValueError, KeyError, TypeError):
            return False
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        if interval != "1D":
            raise LocalCanonicalUnsupportedError("local canonical supports only interval=1D")
        if not codes or len(codes) > MAX_SYMBOLS:
            raise ValueError(f"local canonical requires 1..{MAX_SYMBOLS} symbols")
        normalized = []
        for code in codes:
            symbol = code.strip().upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                raise LocalCanonicalUnsupportedError(
                    f"unsupported local canonical symbol: {symbol!r}"
                )
            if symbol in normalized:
                raise ValueError("local canonical symbols must not contain duplicates")
            normalized.append(symbol)
        requested_fields = set(fields or ["open", "high", "low", "close", "volume"])
        if not requested_fields <= {"open", "high", "low", "close", "volume"}:
            raise LocalCanonicalUnsupportedError("local canonical supports OHLCV fields only")

        start = _strict_date(start_date, "start_date")
        end = _strict_date(end_date, "end_date")
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        dataset = self._dataset()
        if start < dataset.minimum or end > dataset.maximum:
            raise LocalCanonicalIncompleteError(
                "requested range is outside canonical coverage "
                f"[{dataset.minimum.isoformat()}, {dataset.maximum.isoformat()}]"
            )
        years = list(range(start.year, end.year + 1))
        missing_years = [year for year in years if year not in dataset.files_by_year]
        if missing_years:
            raise LocalCanonicalIntegrityError(
                "canonical file inventory does not cover requested year(s): "
                + ", ".join(str(year) for year in missing_years)
            )
        paths = [str(dataset.files_by_year[year]) for year in years]

        try:
            connection = duckdb.connect(":memory:")
        except duckdb.Error as exc:
            raise LocalCanonicalIntegrityError("canonical query engine is unavailable") from exc
        try:
            connection.execute("SET threads=2")
            connection.execute("SET memory_limit='512MB'")
            connection.execute("SET max_temp_directory_size='0B'")
            parameters: list[Any] = [paths, normalized, start.isoformat(), end.isoformat()]
            base_predicate = (
                "FROM read_parquet(?) "
                "WHERE ts_code IN (SELECT unnest(?::VARCHAR[])) "
                "AND trade_date BETWEEN ?::DATE AND ?::DATE"
            )
            duplicate = connection.execute(
                "SELECT ts_code, trade_date, count(*) " + base_predicate +
                " GROUP BY ts_code, trade_date HAVING count(*) > 1 LIMIT 1",
                parameters,
            ).fetchone()
            if duplicate is not None:
                raise LocalCanonicalDuplicateError(
                    f"conflicting natural key: {duplicate[0]}/{duplicate[1]}"
                )
            frame = connection.execute(
                "SELECT ts_code, trade_date, open, high, low, close, vol AS volume "
                + base_predicate
                + " AND price_research_capable AND volume_research_capable"
                + " ORDER BY ts_code, trade_date",
                parameters,
            ).df()
        except LocalCanonicalError:
            raise
        except duckdb.Error as exc:
            raise LocalCanonicalIntegrityError("canonical query failed") from exc
        finally:
            connection.close()

        result: Dict[str, pd.DataFrame] = {}
        provenance = dataset.provenance
        for symbol in normalized:
            selected = frame[frame["ts_code"] == symbol].copy()
            if selected.empty:
                continue
            selected = selected.drop(columns=["ts_code"]).set_index("trade_date")
            selected.index = pd.DatetimeIndex(selected.index, name="trade_date")
            for column in ("open", "high", "low", "close", "volume"):
                selected[column] = selected[column].astype("float64")
            selected.attrs["provenance"] = deepcopy(provenance)
            result[symbol] = selected
        return result
