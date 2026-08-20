#!/usr/bin/env python3
"""Benchmark the read-only local canonical daily loader without network access."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from backtest.loaders.local_canonical_loader import DataLoader, MAX_SYMBOLS  # noqa: E402


MAX_ADDITIONAL_RSS_KIB = 1_048_576
MAX_CACHE_PREPARATION_AGE_SECONDS = 300
CACHE_PREPARATION_SCHEMA = "vibe.local-canonical-cache-preparation.v1"
BENCHMARK_SCHEMA = "vibe.local-canonical-benchmark.v1"
_SYSTEM_CACHE_ADVISOR = object()
_RESTORE_ID_RE = re.compile(r"^restore-[0-9]{8}-[0-9]{6}Z-[0-9a-f]{8,64}$")
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ORACLE_ID_RE = re.compile(r"^oracle-[0-9]{8}-[0-9]{6}Z$")


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    symbol_count: int
    years: int
    max_seconds: float


STANDARD_CASES = (
    BenchmarkCase("one_symbol_five_years", 1, 5, 2.0),
    BenchmarkCase("five_symbols_five_years", 5, 5, 3.0),
    BenchmarkCase("five_hundred_symbols_three_years", 500, 3, 60.0),
)


def _memory_kib() -> dict[str, int | None]:
    values: dict[str, int | None] = {"rss": None, "peak_rss": None}
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            key, _, remainder = line.partition(":")
            if key in {"VmRSS", "VmHWM"}:
                amount = int(remainder.strip().split()[0])
                values["rss" if key == "VmRSS" else "peak_rss"] = amount
    except (OSError, ValueError, IndexError):
        pass
    return values


def _start_for_years(minimum: date, maximum: date, years: int) -> date:
    if years < 1:
        raise ValueError("benchmark years must be positive")
    candidate = date(maximum.year - years + 1, 1, 1)
    return max(minimum, candidate)


def load_symbols(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("symbols file must be readable JSON") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("symbols file must contain a non-empty JSON array")
    symbols = [str(item).strip().upper() for item in value]
    if any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("benchmark symbols must be non-empty and unique")
    return symbols


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _manifest_file_inventory(dataset: Any) -> list[dict[str, Any]]:
    entries = []
    for item in dataset.manifest["files"]:
        entries.append(
            {
                "year": item["year"],
                "manifest_path": item["path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
        )
    return entries


def _recovery_evidence(
    restore_id: str,
    snapshot_id: str,
    oracle_id: str,
) -> dict[str, str]:
    if not isinstance(restore_id, str) or not _RESTORE_ID_RE.fullmatch(restore_id):
        raise ValueError("restore_id_invalid")
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ValueError("snapshot_id_invalid")
    if not isinstance(oracle_id, str) or not _ORACLE_ID_RE.fullmatch(oracle_id):
        raise ValueError("oracle_id_invalid")
    return {
        "restore_id": restore_id,
        "snapshot_id": snapshot_id,
        "oracle_id": oracle_id,
    }


def prepare_page_cache(
    dataset: Any,
    *,
    restore_id: str,
    snapshot_id: str,
    oracle_id: str,
    advisor: Callable[[int, int, int, int], None] | None | object = _SYSTEM_CACHE_ADVISOR,
    advice: int | None | object = _SYSTEM_CACHE_ADVISOR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advise the kernel to evict each manifest Parquet from the page cache.

    The report intentionally records only manifest-relative paths.  Absolute restore
    paths are protected operational details and are never included in the artifact.
    """

    if advisor is _SYSTEM_CACHE_ADVISOR:
        advisor = getattr(os, "posix_fadvise", None)
    if advice is _SYSTEM_CACHE_ADVISOR:
        advice = getattr(os, "POSIX_FADV_DONTNEED", None)

    recovery_evidence = _recovery_evidence(restore_id, snapshot_id, oracle_id)
    inventory = _manifest_file_inventory(dataset)
    files: list[dict[str, Any]] = []
    supported = callable(advisor) and isinstance(advice, int)
    if not supported:
        for item in inventory:
            files.append({**item, "status": "not_attempted", "error": "unsupported"})
        status = "unsupported"
    else:
        for item in inventory:
            entry = dict(item)
            path = dataset.files_by_year[item["year"]]
            descriptor: int | None = None
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                advisor(descriptor, 0, 0, advice)
            except Exception as exc:  # fail closed for platform-specific advisor errors
                entry["status"] = "failed"
                entry["error"] = type(exc).__name__
                if isinstance(exc, OSError) and exc.errno is not None:
                    entry["errno"] = exc.errno
            else:
                entry["status"] = "succeeded"
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            files.append(entry)
        status = (
            "succeeded"
            if files and all(item["status"] == "succeeded" for item in files)
            else "failed"
        )

    completed_at = (now or _utc_now()).astimezone(timezone.utc)
    succeeded = sum(item["status"] == "succeeded" for item in files)
    failed = len(files) - succeeded
    return {
        "schema_version": CACHE_PREPARATION_SCHEMA,
        "preparation_id": str(uuid4()),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "dataset": {
            "canonical_version": dataset.manifest["canonical_version"],
            "files": inventory,
        },
        "recovery_evidence": recovery_evidence,
        "method": {
            "mechanism": "posix_fadvise",
            "advice": "POSIX_FADV_DONTNEED",
            "scope": "each_manifest_parquet",
        },
        "result": {
            "status": status,
            "supported": supported,
            "file_count": len(files),
            "succeeded_file_count": succeeded,
            "failed_file_count": failed,
        },
        "files": files,
    }


def _parse_completed_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("completed_at_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("completed_at_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("completed_at_invalid")
    return parsed.astimezone(timezone.utc)


def validate_cache_preparation(
    dataset: Any,
    report: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int = MAX_CACHE_PREPARATION_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate a separate cache-preparation artifact without trusting its label."""

    artifact_hash = hashlib.sha256(_canonical_json(report).encode("utf-8")).hexdigest()
    summary: dict[str, Any] = {
        "requested": True,
        "validated": False,
        "artifact_sha256": artifact_hash,
        "max_age_seconds": max_age_seconds,
    }
    try:
        if report.get("schema_version") != CACHE_PREPARATION_SCHEMA:
            raise ValueError("schema_version_mismatch")
        preparation_id = report.get("preparation_id")
        if not isinstance(preparation_id, str):
            raise ValueError("preparation_id_invalid")
        try:
            parsed_preparation_id = UUID(preparation_id)
        except ValueError as exc:
            raise ValueError("preparation_id_invalid") from exc
        if parsed_preparation_id.version != 4 or str(parsed_preparation_id) != preparation_id:
            raise ValueError("preparation_id_invalid")
        result = report.get("result")
        if not isinstance(result, Mapping) or result.get("status") != "succeeded":
            raise ValueError("preparation_not_succeeded")
        if result.get("supported") is not True:
            raise ValueError("preparation_not_supported")
        method = report.get("method")
        if not isinstance(method, Mapping) or method != {
            "mechanism": "posix_fadvise",
            "advice": "POSIX_FADV_DONTNEED",
            "scope": "each_manifest_parquet",
        }:
            raise ValueError("method_mismatch")
        report_dataset = report.get("dataset")
        if not isinstance(report_dataset, Mapping):
            raise ValueError("dataset_invalid")
        if report_dataset.get("canonical_version") != dataset.manifest["canonical_version"]:
            raise ValueError("canonical_version_mismatch")
        expected_inventory = _manifest_file_inventory(dataset)
        if report_dataset.get("files") != expected_inventory:
            raise ValueError("file_inventory_mismatch")
        files = report.get("files")
        expected_files = [{**item, "status": "succeeded"} for item in expected_inventory]
        if files != expected_files:
            raise ValueError("per_file_result_mismatch")
        if result.get("file_count") != len(expected_files):
            raise ValueError("file_count_mismatch")
        if result.get("succeeded_file_count") != len(expected_files):
            raise ValueError("succeeded_file_count_mismatch")
        if result.get("failed_file_count") != 0:
            raise ValueError("failed_file_count_mismatch")
        recovery = report.get("recovery_evidence")
        if not isinstance(recovery, Mapping):
            raise ValueError("recovery_evidence_invalid")
        try:
            normalized_recovery = _recovery_evidence(
                recovery.get("restore_id"),
                recovery.get("snapshot_id"),
                recovery.get("oracle_id"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("recovery_evidence_invalid") from exc
        if dict(recovery) != normalized_recovery:
            raise ValueError("recovery_evidence_invalid")
        completed_at = _parse_completed_at(report.get("completed_at"))
        current = (now or _utc_now()).astimezone(timezone.utc)
        age = (current - completed_at).total_seconds()
        if age < -5:
            raise ValueError("preparation_from_future")
        if age > max_age_seconds:
            raise ValueError("preparation_stale")
    except (KeyError, TypeError, ValueError) as exc:
        summary["validation_error"] = str(exc) or type(exc).__name__
        return summary

    summary.update(
        {
            "validated": True,
            "preparation_id": report["preparation_id"],
            "completed_at": report["completed_at"],
            "recovery_evidence": dict(report["recovery_evidence"]),
            "file_count": len(expected_inventory),
        }
    )
    return summary


def run_case(
    loader: DataLoader,
    symbols: Sequence[str],
    case: BenchmarkCase,
    *,
    max_additional_rss_kib: int = MAX_ADDITIONAL_RSS_KIB,
) -> dict[str, Any]:
    if len(symbols) < case.symbol_count:
        raise ValueError(f"case {case.name} requires {case.symbol_count} symbols")
    selected = list(symbols[: case.symbol_count])
    dataset = loader._dataset()
    start = _start_for_years(dataset.minimum, dataset.maximum, case.years)
    end = dataset.maximum
    gc.collect()
    memory_before = _memory_kib()
    started = time.perf_counter()
    row_count = 0
    resolved: set[str] = set()
    versions: set[str] = set()
    chunk_count = 0
    for offset in range(0, len(selected), MAX_SYMBOLS):
        chunk_count += 1
        result = loader.fetch(
            selected[offset : offset + MAX_SYMBOLS],
            start.isoformat(),
            end.isoformat(),
        )
        resolved.update(result)
        for frame in result.values():
            row_count += len(frame)
            versions.add(frame.attrs["provenance"]["canonical_version"])
    elapsed = time.perf_counter() - started
    memory_after = _memory_kib()
    baseline = memory_before["rss"]
    peak = memory_after["peak_rss"]
    additional = None if baseline is None or peak is None else max(0, peak - baseline)
    unresolved = len(set(selected) - resolved)
    passed = (
        elapsed <= case.max_seconds
        and unresolved == 0
        and versions == {dataset.manifest["canonical_version"]}
        and additional is not None
        and additional <= max_additional_rss_kib
    )
    return {
        "name": case.name,
        "request": {
            "symbol_count": len(selected),
            "chunk_count": chunk_count,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        "result": {
            "resolved_symbol_count": len(resolved),
            "unresolved_symbol_count": unresolved,
            "row_count": row_count,
            "canonical_versions": sorted(versions),
        },
        "measurements": {
            "elapsed_seconds": elapsed,
            "baseline_rss_kib": baseline,
            "peak_rss_kib": peak,
            "additional_peak_rss_kib": additional,
        },
        "thresholds": {
            "elapsed_seconds_lte": case.max_seconds,
            "additional_peak_rss_kib_lte": max_additional_rss_kib,
        },
        "passed": passed,
    }


def run_benchmark(
    *,
    catalog_path: Path,
    canonical_root: Path,
    symbols: Sequence[str],
    cases: Sequence[BenchmarkCase] = STANDARD_CASES,
    cache_preparation: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    loader = DataLoader(catalog_path=catalog_path, canonical_root=canonical_root)
    dataset = loader._dataset()
    cache_summary = (
        {
            "requested": False,
            "validated": False,
            "status": "not_requested",
        }
        if cache_preparation is None
        else validate_cache_preparation(dataset, cache_preparation, now=now)
    )
    results = [run_case(loader, symbols, case) for case in cases]
    cold_cache = cache_summary["validated"] is True
    return {
        "schema_version": BENCHMARK_SCHEMA,
        "dataset": {
            "canonical_version": dataset.manifest["canonical_version"],
            "watermark": dataset.manifest["coverage"]["as_of"],
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "method": {
            "network_required": False,
            "reader_chunk_size": MAX_SYMBOLS,
            "os_page_cache": (
                "per_file_posix_fadvise_dontneed" if cold_cache else "uncontrolled"
            ),
            "cold_cache": cold_cache,
            "cache_preparation": cache_summary,
            "guidance": (
                "cache preparation must run before this fresh benchmark container"
                if cold_cache
                else "run a separate opt-in cache preparation before a fresh benchmark container"
            ),
        },
        "cases": results,
        "passed": all(item["passed"] for item in results)
        and (cache_preparation is None or cold_cache),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", type=Path, required=True)
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--symbols-file", type=Path)
    parser.add_argument("--prepare-cold-cache", action="store_true")
    parser.add_argument("--cache-preparation-report", type=Path)
    parser.add_argument("--restore-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--oracle-id")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def _write_report(report: Mapping[str, Any], destination: Path | None) -> str:
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    return serialized


def main() -> int:
    args = _parse_args()
    if args.prepare_cold_cache:
        if args.symbols_file is not None or args.cache_preparation_report is not None:
            raise SystemExit(
                "--prepare-cold-cache cannot be combined with benchmark input options"
            )
        evidence = (args.restore_id, args.snapshot_id, args.oracle_id)
        if any(not value for value in evidence):
            raise SystemExit(
                "--prepare-cold-cache requires --restore-id, --snapshot-id, and --oracle-id"
            )
        loader = DataLoader(
            catalog_path=args.catalog_path,
            canonical_root=args.canonical_root,
        )
        report = prepare_page_cache(
            loader._dataset(),
            restore_id=args.restore_id,
            snapshot_id=args.snapshot_id,
            oracle_id=args.oracle_id,
        )
        print(_write_report(report, args.report))
        return 0 if report["result"]["status"] == "succeeded" else 1

    if args.symbols_file is None:
        raise SystemExit("benchmark mode requires --symbols-file")
    if any((args.restore_id, args.snapshot_id, args.oracle_id)):
        raise SystemExit("recovery evidence options are valid only with --prepare-cold-cache")
    cache_preparation = None
    if args.cache_preparation_report is not None:
        try:
            value = json.loads(args.cache_preparation_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit("cache preparation report must be readable JSON") from exc
        if not isinstance(value, dict):
            raise SystemExit("cache preparation report must contain a JSON object")
        cache_preparation = value
    report = run_benchmark(
        catalog_path=args.catalog_path,
        canonical_root=args.canonical_root,
        symbols=load_symbols(args.symbols_file),
        cache_preparation=cache_preparation,
    )
    print(_write_report(report, args.report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
