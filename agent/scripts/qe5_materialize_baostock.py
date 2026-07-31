#!/usr/bin/env python3
"""Capture BaoStock daily history and materialize a canonical QE5 snapshot."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import sys
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from src.quant_engine import (  # noqa: E402
    BaoStockCaptureError,
    Qe5MaterializationPolicy,
    Qe5UniverseManifest,
    capture_baostock_market,
    materialize_qe5_capture,
)
from src.quant_engine.snapshot_materialization import SHANGHAI  # noqa: E402
from src.research.contracts import canonical_json  # noqa: E402


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _manifest(path: Path) -> Qe5UniverseManifest:
    target = path.absolute()
    if target.is_symlink() or not target.is_file():
        raise ValueError("universe manifest must be a regular non-symlink file")
    if target.stat().st_size > 65_536:
        raise ValueError("universe manifest exceeds 64 KiB")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("universe manifest is not valid UTF-8 JSON") from exc
    try:
        return Qe5UniverseManifest.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"universe manifest failed validation: {exc}") from exc


def parse_args() -> argparse.Namespace:
    today = datetime.now(SHANGHAI).date()
    parser = argparse.ArgumentParser(
        description=(
            "Materialize a provider-neutral QE5 snapshot from BaoStock. "
            "Only explicitly classified long-listed Shanghai/Shenzhen main-board "
            "instruments and dates on/after 2023-08-28 are accepted."
        )
    )
    parser.add_argument("--universe-manifest", type=Path, required=True)
    parser.add_argument("--start-date", type=_date, required=True)
    parser.add_argument("--end-date", type=_date, required=True)
    parser.add_argument("--as-of", type=_date, default=today)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--publish-root",
        type=Path,
        help=(
            "Optional Vibe home root. Publishes the content-addressed snapshot "
            "and ResearchSpec/DataSnapshotRef after the audit bundle succeeds."
        ),
    )
    parser.add_argument("--commission-tenths-bps", type=int, default=30)
    parser.add_argument("--minimum-commission-fen", type=int, default=500)
    parser.add_argument("--sell-tax-tenths-bps", type=int, default=50)
    parser.add_argument("--transfer-fee-tenths-bps", type=int, default=1)
    parser.add_argument("--max-participation-bps", type=int, default=1_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        universe = _manifest(args.universe_manifest)
        policy = Qe5MaterializationPolicy(
            commission_tenths_bps=args.commission_tenths_bps,
            minimum_commission_fen=args.minimum_commission_fen,
            sell_tax_tenths_bps=args.sell_tax_tenths_bps,
            transfer_fee_tenths_bps=args.transfer_fee_tenths_bps,
            max_participation_bps=args.max_participation_bps,
        )
        with redirect_stdout(sys.stderr):
            capture = capture_baostock_market(
                universe,
                start_date=args.start_date,
                end_date=args.end_date,
                as_of=args.as_of,
            )
        result = materialize_qe5_capture(
            capture,
            args.output_dir,
            policy=policy,
            publish_root=args.publish_root,
        )
    except (BaoStockCaptureError, OSError, ValueError) as exc:
        print(f"QE5 BaoStock materialization failed: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(result.manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
