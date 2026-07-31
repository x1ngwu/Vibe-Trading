#!/usr/bin/env python3
"""QE5 PR-03 deterministic 500-symbol, three-year backtest benchmark.

The generated market is synthetic and is used only to measure the formal
offline worker at the target shape.  It is never presented as historical
market data.  Correctness remains covered by the smaller frozen/golden
fixtures, including real corporate-action windows.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from src.quant_engine import (  # noqa: E402
    EngineIdentity,
    QUANTAXIS_ENGINE_COMMIT,
    QuantaxisAdapter,
    WorkerConfig,
    WorkerRunner,
    write_quantaxis_backtest_snapshot,
)
from src.research.contracts import (  # noqa: E402
    CostSpec,
    DataSnapshotRef,
    EngineIdentitySpec,
    EvaluationSpec,
    ResearchSpec,
    ResourceLimits,
    RiskSpec,
    canonical_json,
    create_research_object,
)
from src.strategy_spec import (  # noqa: E402
    StrategyDraftProposal,
    StrategyDraftResult,
    StrategyTemplateSource,
    StrategyVersionStore,
    TopNRebalanceTemplate,
    build_strategy_template,
    compile_strategy_template,
)

CREATED_AT = datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
START_DATE = date(2023, 1, 2)
TRAIN_END = date(2023, 12, 29)
VALIDATION_END = date(2024, 12, 31)
END_DATE = date(2025, 12, 31)
PR03_MAX_SECONDS = 60.0
PR03_MAX_RSS_KIB = 1_048_576
WORKER_MEMORY_BYTES = 2_147_483_648


def _symbols(count: int) -> tuple[str, ...]:
    if not 2 <= count <= 500:
        raise ValueError("symbol count must be between 2 and 500")
    split = count // 2
    values = [
        *(f"{index:06d}.SZ" for index in range(1, split + 1)),
        *(f"{600000 + index:06d}.SH" for index in range(1, count - split + 1)),
    ]
    return tuple(sorted(values))


def _weekdays(start_date: date, end_date: date) -> tuple[date, ...]:
    values: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def build_benchmark_snapshot(
    *,
    symbol_count: int = 500,
    start_date: date = START_DATE,
    end_date: date = END_DATE,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[date, ...]]:
    """Build a deterministic target-shape snapshot without provider access."""

    symbols = _symbols(symbol_count)
    dates = _weekdays(start_date, end_date)
    bars: list[dict[str, Any]] = []
    for symbol_index, symbol in enumerate(symbols):
        prior_close = 800 + symbol_index * 3
        market_rule_id = (
            "sz-main-10pct-pr03-v1"
            if symbol.endswith(".SZ")
            else "sh-main-10pct-pr03-v1"
        )
        for day_index, trade_date in enumerate(dates):
            cycle = (day_index % 31) - 15
            close_fen = max(100, prior_close + ((symbol_index % 7) - 3) + cycle // 5)
            open_fen = max(100, prior_close + ((day_index + symbol_index) % 5) - 2)
            high_fen = max(open_fen, close_fen) + 3
            low_fen = max(1, min(open_fen, close_fen) - 3)
            bars.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "known_at": f"{trade_date.isoformat()}T15:01:00+08:00",
                    "symbol": symbol,
                    "open_fen": open_fen,
                    "high_fen": high_fen,
                    "low_fen": low_fen,
                    "close_fen": close_fen,
                    "signal_close_fen": close_fen,
                    "limit_reference_fen": prior_close,
                    "volume_shares": 20_000_000 + symbol_index * 1_000,
                    "status": "traded",
                    "is_st": False,
                    "listing_trade_day_number": 5_000 + day_index,
                    "market_rule_id": market_rule_id,
                    "features": {},
                }
            )
            prior_close = close_fen
    return (
        {
            "schema_version": "vibe.quantaxis-backtest-snapshot.v1",
            "price_semantics": {
                "execution_price_adjustment": "raw",
                "signal_price_adjustment": "qfq",
                "corporate_action_mode": "explicit",
            },
            "rule_table": {
                "schema_version": "vibe.cn-equity-rule-table.v1",
                "version": "cn-equity-pr03-2023-2025-v1",
                "fee_schedule": {
                    "effective_from": start_date.isoformat(),
                    "effective_to": end_date.isoformat(),
                    "commission_tenths_bps": 30,
                    "minimum_commission_fen": 500,
                    "sell_tax_tenths_bps": 50,
                    "transfer_fee_tenths_bps": 1,
                    "rule_version": "cn-equity-pr03-fees-v1",
                },
                "market_rules": [
                    {
                        "rule_id": "sz-main-10pct-pr03-v1",
                        "effective_from": start_date.isoformat(),
                        "effective_to": end_date.isoformat(),
                        "board": "sz_main",
                        "is_st": False,
                        "listing_day_min": 1,
                        "listing_day_max": None,
                        "limit_up_bps": 1_000,
                        "limit_down_bps": 1_000,
                        "tick_fen": 1,
                    },
                    {
                        "rule_id": "sh-main-10pct-pr03-v1",
                        "effective_from": start_date.isoformat(),
                        "effective_to": end_date.isoformat(),
                        "board": "sh_main",
                        "is_st": False,
                        "listing_day_min": 1,
                        "listing_day_max": None,
                        "limit_up_bps": 1_000,
                        "limit_down_bps": 1_000,
                        "tick_fen": 1,
                    },
                ],
                "max_participation_bps": 1_000,
            },
            "instruments": [
                {
                    "symbol": symbol,
                    "board": "sz_main" if symbol.endswith(".SZ") else "sh_main",
                    "listing_date": "2000-01-01",
                    "delisting_date": None,
                }
                for symbol in symbols
            ],
            "calendar": [
                {"trade_date": value.isoformat(), "is_open": True}
                for value in dates
            ],
            "bars": bars,
            "corporate_actions": [],
        },
        symbols,
        dates,
    )


class _RecordingRunner:
    def __init__(self, runner: WorkerRunner) -> None:
        self.runner = runner
        self.config = runner.config
        self.last_run = None

    def run(self, **kwargs):
        self.last_run = self.runner.run(**kwargs)
        return self.last_run


def _run(
    *,
    worker_python: Path,
    output_dir: Path,
    symbol_count: int,
) -> dict[str, Any]:
    payload, symbols, dates = build_benchmark_snapshot(symbol_count=symbol_count)
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    snapshot_path = output_dir / "qe5-pr03-snapshot.json"
    materialize_started = time.monotonic()
    snapshot_sha256 = write_quantaxis_backtest_snapshot(payload, snapshot_path)
    materialize_seconds = time.monotonic() - materialize_started
    snapshot_size_bytes = snapshot_path.stat().st_size
    # The worker reads the immutable artifact itself. Release the large
    # generator object before dispatch so the benchmark measures the same
    # parent/worker memory overlap as the product service, not a test-only copy.
    del payload
    gc.collect()

    research = create_research_object(
        ResearchSpec(
            symbols=(symbols[0],),
            as_of=END_DATE,
            lookback_days=(20,),
            candidate_universe="qe5-pr03-synthetic-500",
            requested_outputs=("strategy", "backtest"),
        ),
        created_at=CREATED_AT,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256=snapshot_sha256,
            as_of=END_DATE,
            start_date=START_DATE,
            end_date=END_DATE,
            adjustment="qfq",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("qe5_pr03_synthetic",),
            actual_sources={
                symbol: "qe5_pr03_synthetic" for symbol in symbols
            },
            anomalies=("synthetic_performance_fixture:not_market_data",),
        ),
        parent_refs=(research.ref(),),
        created_at=CREATED_AT,
    )
    source = StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=symbols,
    )
    template = TopNRebalanceTemplate(
        title="QE5 PR-03 500-stock benchmark",
        ranking_field="ma_20",
        ranking_direction="descending",
        top_n=5,
        rebalance="monthly",
        max_positions=5,
        max_position_weight=0.19,
        cash_buffer_weight=0.05,
        costs=CostSpec(
            commission_bps=3.0,
            minimum_commission=5.0,
            sell_tax_bps=5.0,
            transfer_fee_bps=0.1,
            slippage_bps=5.0,
            rule_version="cn-equity-pr03-fees-v1",
        ),
        risk=RiskSpec(max_drawdown_stop=0.2, max_turnover=12.0),
        evaluation=EvaluationSpec(
            train_end=TRAIN_END,
            validation_end=VALIDATION_END,
            test_end=END_DATE,
            benchmark="000300.SH",
        ),
    )
    build = build_strategy_template(
        template,
        source=source,
        created_at=CREATED_AT,
    )
    limits = ResourceLimits(
        timeout_seconds=PR03_MAX_SECONDS,
        max_stdout_bytes=8_388_608,
        max_stderr_bytes=2_097_152,
        memory_bytes=WORKER_MEMORY_BYTES,
    )
    compilation = compile_strategy_template(
        build,
        snapshot=snapshot,
        engine=EngineIdentitySpec(
            name="quantaxis",
            commit=QUANTAXIS_ENGINE_COMMIT,
        ),
        resource_limits=limits,
        random_seed=0,
        created_at=CREATED_AT,
    )
    version_db = output_dir / "strategy-versions.db"
    draft_result = StrategyDraftResult(
        status="ready",
        request_sha256="0" * 64,
        model_response_sha256="1" * 64,
        proposal=StrategyDraftProposal(
            template_id="top_n_rebalance",
            title=template.title,
            ranking_field=template.ranking_field,
            ranking_direction=template.ranking_direction,
            top_n=template.top_n,
            rebalance=template.rebalance,
            max_positions=template.max_positions,
            max_position_weight=template.max_position_weight,
            cash_buffer_weight=template.cash_buffer_weight,
            max_drawdown_stop=template.risk.max_drawdown_stop,
            max_turnover=template.risk.max_turnover,
            train_end=template.evaluation.train_end,
            validation_end=template.evaluation.validation_end,
            test_end=template.evaluation.test_end,
            benchmark=template.evaluation.benchmark,
        ),
        template=template,
        build=build,
    )
    with StrategyVersionStore(version_db) as versions:
        version, draft_head = versions.create_initial_version(
            stream_id="qe5-pr03-benchmark",
            owner_scope="household:v1",
            result=draft_result,
            created_at=CREATED_AT,
        )
        card, awaiting = versions.prepare_confirmation(
            stream_id=version.stream_id,
            expected_head=draft_head,
            issued_at=CREATED_AT + timedelta(minutes=1),
            expires_at=CREATED_AT + timedelta(minutes=11),
        )
        receipt = versions.confirm(
            stream_id=version.stream_id,
            expected_head=awaiting,
            confirmation_hash=card.confirmation_hash,
            idempotency_key="qe5-pr03-confirm",
            actor_id="benchmark",
            confirmed_at=CREATED_AT + timedelta(minutes=2),
        )
        head = versions.get_head(version.stream_id)
    assert head is not None

    agent_root = Path(__file__).resolve().parents[1]
    base_runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT),
            # Keep the venv launcher path. Resolving its python symlink would
            # bypass pyvenv.cfg and silently lose the pinned site-packages.
            python=worker_python.absolute(),
            script=(
                agent_root / "engine_workers" / "quantaxis" / "worker.py"
            ).resolve(),
            snapshot_root=output_dir.resolve(),
        ),
        common_runtime=(
            agent_root / "engine_workers" / "common"
        ).resolve(),
    )
    runner = _RecordingRunner(base_runner)
    started = time.monotonic()
    result = QuantaxisAdapter(runner).backtest(
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=100_000_000,
    )
    end_to_end_seconds = time.monotonic() - started
    run = runner.last_run
    assert run is not None
    peak_rss_kib = run.peak_rss_kib
    passed = (
        run.elapsed_seconds <= PR03_MAX_SECONDS
        and peak_rss_kib is not None
        and peak_rss_kib <= PR03_MAX_RSS_KIB
    )
    return {
        "schema_version": "vibe.qe5-pr03-benchmark.v1",
        "fixture": {
            "kind": "deterministic_synthetic_not_market_data",
            "symbol_count": len(symbols),
            "trading_day_count": len(dates),
            "bar_count": len(symbols) * len(dates),
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "snapshot_sha256": snapshot_sha256,
            "snapshot_size_bytes": snapshot_size_bytes,
        },
        "engine": {
            "name": "quantaxis",
            "commit": QUANTAXIS_ENGINE_COMMIT,
            "version": result.worker.engine_version,
            "worker_python": str(worker_python.absolute()),
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "measurements": {
            "materialize_seconds": materialize_seconds,
            "worker_seconds": run.elapsed_seconds,
            "end_to_end_seconds": end_to_end_seconds,
            "worker_peak_rss_kib": peak_rss_kib,
            "event_count": len(result.worker.events),
            "ledger_entry_count": len(result.ledger.entries),
        },
        "thresholds": {
            "worker_seconds_lte": PR03_MAX_SECONDS,
            "worker_peak_rss_kib_lte": PR03_MAX_RSS_KIB,
            "worker_address_space_limit_bytes": WORKER_MEMORY_BYTES,
        },
        "passed": passed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--worker-python",
        type=Path,
        default=Path(
            os.environ.get("VIBE_QE0_QUANTAXIS_PYTHON", "")
        ),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--symbols", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not str(args.worker_python):
        raise SystemExit(
            "set VIBE_QE0_QUANTAXIS_PYTHON or pass --worker-python"
        )
    if args.output_dir is None:
        with tempfile.TemporaryDirectory(prefix="vibe-qe5-pr03-") as raw:
            report = _run(
                worker_python=args.worker_python,
                output_dir=Path(raw) / "artifacts",
                symbol_count=args.symbols,
            )
    else:
        report = _run(
            worker_python=args.worker_python,
            output_dir=args.output_dir,
            symbol_count=args.symbols,
        )
    serialized = canonical_json(report)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        temporary.replace(args.report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
