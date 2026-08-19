#!/usr/bin/env python3
"""Benchmark the read-only local canonical daily loader without network access."""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from backtest.loaders.local_canonical_loader import DataLoader, MAX_SYMBOLS  # noqa: E402


MAX_ADDITIONAL_RSS_KIB = 1_048_576


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
) -> dict[str, Any]:
    loader = DataLoader(catalog_path=catalog_path, canonical_root=canonical_root)
    results = [run_case(loader, symbols, case) for case in cases]
    dataset = loader._dataset()
    return {
        "schema_version": "vibe.local-canonical-benchmark.v1",
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
            "os_page_cache": "uncontrolled",
            "guidance": "run in a fresh process; record cold-cache preparation separately",
        },
        "cases": results,
        "passed": all(item["passed"] for item in results),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", type=Path, required=True)
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--symbols-file", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = run_benchmark(
        catalog_path=args.catalog_path,
        canonical_root=args.canonical_root,
        symbols=load_symbols(args.symbols_file),
    )
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, args.report)
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
