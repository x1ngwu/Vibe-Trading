#!/usr/bin/env python3
"""PR-02 deterministic QE3 small/medium/target-scale performance baseline."""

from __future__ import annotations

import argparse
import math
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))

from src.research import (  # noqa: E402
    BusinessFeatureRecord,
    BusinessFeatureSnapshot,
    BusinessFieldProvenance,
    ChannelWeights,
    DataSnapshotRef,
    FactorFeatureRecord,
    FactorFeatureSnapshot,
    FactorFeatureValue,
    PeerSet,
    PriceVolumeFeatureRecord,
    PriceVolumeFeatureSnapshot,
    PriceVolumeObservation,
    ResearchSpec,
    SimilaritySensitivityScenario,
    build_business_similarity_object,
    build_factor_similarity_object,
    build_price_volume_similarity_object,
    build_three_channel_similarity,
    canonical_json,
    canonical_sha256,
    create_research_object,
)

AS_OF = date(2026, 7, 24)
CREATED_AT = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
KNOWN_AT = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
METRIC_WEIGHTS = {
    "price_path": 1.0,
    "return_correlation": 1.0,
    "volatility": 1.0,
    "drawdown": 1.0,
    "volume_path": 1.0,
    "turnover_path": 1.0,
    "price_volume_correlation": 1.0,
}
FACTOR_WEIGHTS = {
    "drawdown_20d": 1.0,
    "momentum_20d": 1.0,
    "turnover_change_20d": 1.0,
    "volatility_20d": 1.0,
}
BASE_WEIGHTS = ChannelWeights(business=0.3, factor=0.3, price_volume=0.4)
PERTURBED_WEIGHTS = ChannelWeights(business=0.2, factor=0.3, price_volume=0.5)


def _symbol(index: int) -> str:
    return f"{600000 + index:06d}.SH"


def _case(size: int) -> dict[str, Any]:
    if not 3 <= size <= 300:
        raise ValueError("benchmark size must be between 3 and 300")
    symbols = tuple(_symbol(index) for index in range(size))
    target = symbols[0]
    members = symbols[1:]
    research = create_research_object(
        ResearchSpec(
            symbols=(target,),
            as_of=AS_OF,
            lookback_days=(20, 60),
            candidate_universe=f"csi300@{AS_OF.isoformat()}",
        ),
        created_at=CREATED_AT,
    )
    data_snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256=canonical_sha256({"size": size, "kind": "benchmark"}),
            as_of=AS_OF,
            start_date=date(2026, 4, 27),
            end_date=AS_OF,
            adjustment="qfq",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("benchmark",),
            actual_sources={symbol: "benchmark" for symbol in symbols},
        ),
        parent_refs=(research.ref(),),
        created_at=CREATED_AT,
    )
    peers = create_research_object(
        PeerSet(
            research_spec_ref=research.ref(),
            data_snapshot_ref=data_snapshot.ref(),
            target_symbol=target,
            members=members,
            included_reasons={symbol: ("benchmark_peer",) for symbol in members},
            excluded_reasons={target: ("target_symbol",)},
            coverage=1.0,
        ),
        parent_refs=(research.ref(), data_snapshot.ref()),
        created_at=CREATED_AT,
    )
    provenance = {
        name: BusinessFieldProvenance(
            source="benchmark",
            source_version="qe3-pr02-v1",
            known_at=KNOWN_AT,
            source_fields=(field,),
        )
        for name, field in {
            "industry": "benchmark.industry",
            "market_cap": "benchmark.market_cap",
            "liquidity": "benchmark.liquidity",
            "listing_age": "benchmark.listing_date",
        }.items()
    }
    business_records = []
    factor_records = []
    price_records = []
    dates = tuple(date(2026, 5, 4 + index) for index in range(20))
    for index, symbol in enumerate(symbols):
        phase = (index % 17) / 1000.0
        business_records.append(
            BusinessFeatureRecord(
                symbol=symbol,
                industry=f"industry-{index % 10}",
                market_cap_cny=10_000_000_000.0 * (1.0 + index / size),
                average_daily_turnover_cny=100_000_000.0 * (1.0 + index / size),
                liquidity_observation_count=20,
                listing_date=date(2000 + index % 20, 1, 1),
                provenance=provenance,
            )
        )
        factor_values = {
            "drawdown_20d": -0.01 - phase,
            "momentum_20d": 0.02 + phase,
            "turnover_change_20d": 0.03 - phase,
            "volatility_20d": 0.01 + phase,
        }
        factor_records.append(
            FactorFeatureRecord(
                symbol=symbol,
                values=tuple(
                    FactorFeatureValue(
                        factor_id=factor_id,
                        value=value,
                        source="benchmark",
                        source_version="qe3-pr02-v1",
                        known_at=KNOWN_AT,
                        source_fields=(f"benchmark.{factor_id}",),
                    )
                    for factor_id, value in factor_values.items()
                ),
            )
        )
        price_records.append(
            PriceVolumeFeatureRecord(
                symbol=symbol,
                source="benchmark",
                source_version="qe3-pr02-v1",
                observations=tuple(
                    PriceVolumeObservation(
                        trade_date=trade_date,
                        close=(10.0 + index / 100.0) * (1.0 + day / 1000.0 + phase),
                        volume=1_000_000.0 + index * 100.0 + day * 1000.0,
                        amount=10_000_000.0 + index * 1000.0 + day * 10_000.0,
                    )
                    for day, trade_date in enumerate(dates)
                ),
            )
        )
    business = BusinessFeatureSnapshot(
        snapshot_id=f"qe3-pr02-business-{size}",
        as_of=AS_OF,
        liquidity_window_days=20,
        records=tuple(business_records),
    )
    factors = FactorFeatureSnapshot(
        snapshot_id=f"qe3-pr02-factors-{size}",
        as_of=AS_OF,
        records=tuple(factor_records),
    )
    price = PriceVolumeFeatureSnapshot(
        snapshot_id=f"qe3-pr02-price-{size}",
        data_snapshot_sha256=data_snapshot.payload.snapshot_sha256,
        as_of=AS_OF,
        window_days=20,
        records=tuple(price_records),
    )
    shorter_price = PriceVolumeFeatureSnapshot(
        snapshot_id=f"qe3-pr02-price-short-{size}",
        data_snapshot_sha256=data_snapshot.payload.snapshot_sha256,
        as_of=AS_OF,
        window_days=19,
        records=tuple(
            record.model_copy(update={"observations": record.observations[1:]})
            for record in price.records
        ),
    )
    return {
        "research": research,
        "data_snapshot": data_snapshot,
        "peers": peers,
        "business": business,
        "factors": factors,
        "price": price,
        "shorter_price": shorter_price,
    }


def _execute(case: dict[str, Any]) -> str:
    research = case["research"]
    data_snapshot = case["data_snapshot"]
    peers = case["peers"]
    top_n = len(peers.payload.members)
    business_result = build_business_similarity_object(
        research,
        data_snapshot,
        peers,
        case["business"],
        top_n=top_n,
        min_coverage=0.5,
        created_at=CREATED_AT,
    )
    factor_result = build_factor_similarity_object(
        research,
        data_snapshot,
        peers,
        case["factors"],
        factor_weights=FACTOR_WEIGHTS,
        top_n=top_n,
        min_coverage=0.5,
        created_at=CREATED_AT,
    )
    price_result = build_price_volume_similarity_object(
        research,
        data_snapshot,
        peers,
        case["price"],
        metric_weights=METRIC_WEIGHTS,
        top_n=top_n,
        min_coverage=0.5,
        created_at=CREATED_AT,
    )
    shorter_result = build_price_volume_similarity_object(
        research,
        data_snapshot,
        peers,
        case["shorter_price"],
        metric_weights=METRIC_WEIGHTS,
        top_n=top_n,
        min_coverage=0.5,
        created_at=CREATED_AT,
    )
    combined = build_three_channel_similarity(
        research,
        data_snapshot,
        peers,
        business_result,
        factor_result,
        price_result,
        weights=BASE_WEIGHTS,
        top_n=min(10, top_n),
        min_coverage=0.5,
        sensitivity_scenarios=(
            SimilaritySensitivityScenario(
                scenario_id="pr02-weights",
                kind="weights",
                weights=PERTURBED_WEIGHTS,
                business_result=business_result,
                factor_result=factor_result,
                price_volume_result=price_result,
            ),
            SimilaritySensitivityScenario(
                scenario_id="pr02-window",
                kind="window",
                weights=BASE_WEIGHTS,
                business_result=business_result,
                factor_result=factor_result,
                price_volume_result=shorter_result,
            ),
        ),
    )
    return canonical_sha256(combined)


def run_baseline(
    *,
    sizes: tuple[int, ...] = (12, 128, 300),
    repeats: int = 3,
) -> dict[str, Any]:
    results = []
    for size in sizes:
        case = _case(size)
        expected = _execute(case)
        durations = []
        peaks = []
        for _ in range(repeats):
            tracemalloc.start()
            started = time.perf_counter()
            actual = _execute(case)
            duration = (time.perf_counter() - started) * 1000.0
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if actual != expected:
                raise RuntimeError("QE3 benchmark result changed across repeats")
            durations.append(duration)
            peaks.append(peak / 1024.0)
        ordered = sorted(durations)
        p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
        results.append(
            {
                "symbol_count": size,
                "candidate_count": size - 1,
                "repeats": repeats,
                "median_ms": round(statistics.median(durations), 3),
                "p95_ms": round(ordered[p95_index], 3),
                "peak_kib": round(max(peaks), 3),
                "result_sha256": expected,
            }
        )
    try:
        source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_DIR,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001 - metadata remains explicit
        source_commit = "unavailable"
    return {
        "schema_version": "vibe.qe3-performance-baseline.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    result = run_baseline(repeats=args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    args.output.write_text(canonical_json(result), encoding="utf-8")
    args.output.chmod(0o600)
    print(args.output)


if __name__ == "__main__":
    main()
