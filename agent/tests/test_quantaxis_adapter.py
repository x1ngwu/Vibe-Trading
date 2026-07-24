"""QE2 tests for the typed QUANTAXIS operation boundary and real pinned worker."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from src.quant_engine import (
    EngineIdentity,
    QUANTAXIS_ENGINE_COMMIT,
    QuantaxisAdapter,
    QuantaxisFactorSpec,
    WorkerConfig,
    WorkerExecutionError,
    WorkerRunner,
    compute_snapshot_sha256,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = AGENT_ROOT / "tests" / "fixtures" / "quant_engine" / "qe2_quantaxis_operations_v1.json"
COMMON_RUNTIME = (AGENT_ROOT / "engine_workers" / "common").resolve()


class _FakeRunner:
    def __init__(self, *, commit: str = QUANTAXIS_ENGINE_COMMIT) -> None:
        self.config = SimpleNamespace(engine=EngineIdentity("quantaxis", commit))
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            response={
                "status": "ok",
                "error": None,
                "result": {
                    "operation_schema": f"test.{kwargs['operation']}.v1",
                    "snapshot_sha256": kwargs["snapshot_sha256"],
                },
            }
        )


def test_adapter_binds_snapshot_hash_and_strict_factor_payload(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    shutil.copyfile(FIXTURE, snapshot)
    runner = _FakeRunner()
    adapter = QuantaxisAdapter(runner)  # type: ignore[arg-type]

    result = adapter.compute_factors(
        request_id="qe2-adapter-factor",
        symbol="301336.SZ",
        factors=(QuantaxisFactorSpec(name="ma", window=2),),
        snapshot_path=snapshot,
    )

    assert result["snapshot_sha256"] == compute_snapshot_sha256(snapshot)
    assert runner.calls == [
        {
            "request_id": "qe2-adapter-factor",
            "operation": "compute_factors",
            "payload": {
                "symbol": "301336.SZ",
                "factors": [{"name": "ma", "window": 2}],
            },
            "snapshot_path": str(snapshot.absolute()),
            "snapshot_sha256": compute_snapshot_sha256(snapshot),
            "timeout_seconds": 30.0,
        }
    ]

    with pytest.raises(ValueError, match="must be unique"):
        adapter.compute_factors(
            request_id="qe2-adapter-duplicate",
            symbol="301336.SZ",
            factors=(
                QuantaxisFactorSpec(name="ema", window=3),
                QuantaxisFactorSpec(name="ema", window=3),
            ),
            snapshot_path=snapshot,
        )


def test_adapter_rejects_any_unreviewed_quantaxis_commit() -> None:
    with pytest.raises(ValueError, match="audited QUANTAXIS commit"):
        QuantaxisAdapter(_FakeRunner(commit="0" * 40))  # type: ignore[arg-type]


def _real_adapter(tmp_path: Path) -> tuple[QuantaxisAdapter, Path]:
    python_text = os.environ.get("VIBE_QE0_QUANTAXIS_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_QUANTAXIS_PYTHON to run pinned QUANTAXIS operations")
    snapshot = tmp_path / "qe2_quantaxis_operations_v1.json"
    shutil.copyfile(FIXTURE, snapshot)
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(AGENT_ROOT / "engine_workers" / "quantaxis" / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_RUNTIME,
    )
    return QuantaxisAdapter(runner), snapshot


@pytest.mark.integration
def test_qe2_real_quantaxis_operations_are_offline_content_bound_and_deterministic(
    tmp_path: Path,
) -> None:
    adapter, snapshot = _real_adapter(tmp_path)

    qfq = adapter.adjust_prices(
        request_id="qe2-qfq",
        symbol="301336.SZ",
        adjustment="qfq",
        snapshot_path=snapshot,
    )
    hfq = adapter.adjust_prices(
        request_id="qe2-hfq",
        symbol="301336.SZ",
        adjustment="hfq",
        snapshot_path=snapshot,
    )
    calendar = adapter.trading_calendar(
        request_id="qe2-calendar",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 6),
        snapshot_path=snapshot,
    )
    factors = adapter.compute_factors(
        request_id="qe2-factors",
        symbol="301336.SZ",
        factors=(
            QuantaxisFactorSpec(name="ma", window=2),
            QuantaxisFactorSpec(name="ema", window=3),
        ),
        snapshot_path=snapshot,
    )

    assert qfq["price_anchor_date"] == qfq["volume_anchor_date"] == "2026-06-01"
    assert hfq["price_anchor_date"] == hfq["volume_anchor_date"] == "2026-05-27"
    assert qfq["rows"][1]["close"] == pytest.approx(40.05, abs=0.01)
    assert qfq["rows"][1]["volume"] == pytest.approx(6176 * 1.2976135)
    assert hfq["rows"][2]["volume"] == pytest.approx(11727 / 1.2976135)
    assert [item["amount"] for item in qfq["rows"]] == [
        56929.386,
        32269.275,
        46374.774,
        85720.907,
    ]

    assert calendar["truth_source"] == "content_bound_snapshot"
    assert calendar["open_dates"] == ["2026-05-06"]
    assert [item["trade_date"] for item in calendar["quantaxis_oracle_mismatches"]] == [
        "2026-05-04",
        "2026-05-05",
    ]
    assert factors["input_price_basis"] == "raw"
    assert factors["rows"] == [
        {"trade_date": "2026-05-27", "ma_2": None, "ema_3": None},
        {"trade_date": "2026-05-28", "ma_2": 52.2, "ema_3": 52.2066666667},
        {"trade_date": "2026-05-29", "ma_2": 46.25, "ema_3": 45.3914285714},
        {"trade_date": "2026-06-01", "ma_2": 40.74, "ema_3": 43.156},
    ]

    replay = adapter.compute_factors(
        request_id="qe2-factors-replay",
        symbol="301336.SZ",
        factors=(
            QuantaxisFactorSpec(name="ma", window=2),
            QuantaxisFactorSpec(name="ema", window=3),
        ),
        snapshot_path=snapshot,
    )
    assert replay == factors


@pytest.mark.integration
def test_qe2_formal_operation_requires_a_snapshot(tmp_path: Path) -> None:
    adapter, _ = _real_adapter(tmp_path)
    with pytest.raises(WorkerExecutionError) as raised:
        adapter.runner.run(
            request_id="qe2-missing-snapshot",
            operation="adjust_prices",
            payload={"symbol": "301336.SZ", "adjustment": "qfq"},
        )
    assert raised.value.code == "SNAPSHOT_REQUIRED"
