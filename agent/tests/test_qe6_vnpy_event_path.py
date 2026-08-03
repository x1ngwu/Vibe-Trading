"""QE6-1 exact QE5 request mapping into the pinned vn.py EventEngine."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from src.quant_engine import (
    EngineIdentity,
    QUANTAXIS_ENGINE_COMMIT,
    VNPY_ENGINE_COMMIT,
    VNPY_ENGINE_VERSION,
    VNPY_SOURCE_SHA256,
    VnpyOracleAdapter,
    WorkerConfig,
    WorkerRunner,
    compute_snapshot_sha256,
    write_quantaxis_backtest_snapshot,
)
from src.research.contracts import (
    CostSpec,
    DataSnapshotRef,
    EngineIdentitySpec,
    EvaluationSpec,
    ResearchSpec,
    ResourceLimits,
    RiskSpec,
    canonical_sha256,
    create_research_object,
)
from src.strategy_spec import (
    StrategyTemplateSource,
    TopNRebalanceTemplate,
    build_strategy_template,
    compile_strategy_template,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = AGENT_ROOT / "engine_workers" / "vnpy"
COMMON_DIR = AGENT_ROOT / "engine_workers" / "common"
for path in (str(COMMON_DIR), str(WORKER_DIR)):
    if path not in os.sys.path:
        os.sys.path.insert(0, path)

from formal_event_path import build_event_replay_handler  # noqa: E402
from worker_runtime import WorkerError  # noqa: E402


_T0 = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
_SYMBOL = "600001.SH"


class _FakeEvent:
    def __init__(self, event_type: str, data: Mapping[str, Any]) -> None:
        self.type = event_type
        self.data = data


class _FakeEventEngine:
    def __init__(self, *, interval: float) -> None:
        assert interval == 0.01
        self.handlers: dict[str, Callable[[Any], None]] = {}

    def register(self, event_type: str, handler: Callable[[Any], None]) -> None:
        self.handlers[event_type] = handler

    def start(self) -> None:
        return None

    def put(self, event: _FakeEvent) -> None:
        self.handlers[event.type](event)

    def stop(self) -> None:
        return None


def _boundary() -> Mapping[str, Any]:
    return {
        "version": VNPY_ENGINE_VERSION,
        "source_sha256": VNPY_SOURCE_SHA256,
        "Event": _FakeEvent,
        "EventEngine": _FakeEventEngine,
    }


def _snapshot_payload() -> dict[str, Any]:
    return {
        "schema_version": "vibe.quantaxis-backtest-snapshot.v1",
        "price_semantics": {
            "execution_price_adjustment": "raw",
            "signal_price_adjustment": "qfq",
            "corporate_action_mode": "explicit",
        },
        "rule_table": {},
        "instruments": [
            {
                "symbol": _SYMBOL,
                "board": "sh_main",
                "listing_date": "2000-01-01",
                "delisting_date": None,
            }
        ],
        "calendar": [],
        "bars": [],
        "corporate_actions": [],
    }


def _compilation(
    tmp_path: Path,
    *,
    engine_name: str = "quantaxis",
    engine_commit: str = QUANTAXIS_ENGINE_COMMIT,
    costs: CostSpec | None = None,
):
    snapshot_path = tmp_path / f"snapshot-{engine_name}.json"
    snapshot_sha256 = write_quantaxis_backtest_snapshot(
        _snapshot_payload(),
        snapshot_path,
    )
    research = create_research_object(
        ResearchSpec(
            symbols=(_SYMBOL,),
            as_of=date(2025, 1, 7),
            lookback_days=(20,),
            candidate_universe="qe6-worker-fixture",
            requested_outputs=("strategy", "backtest"),
        ),
        created_at=_T0,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256=snapshot_sha256,
            as_of=date(2025, 1, 7),
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 7),
            adjustment="qfq",
            symbols=(_SYMBOL,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("qe6-fixture",),
            actual_sources={_SYMBOL: "qe6-fixture"},
        ),
        parent_refs=(research.ref(),),
        created_at=_T0,
    )
    source = StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=(_SYMBOL,),
    )
    build = build_strategy_template(
        TopNRebalanceTemplate(
            title="QE6-1 event path",
            rebalance="daily",
            max_positions=1,
            max_position_weight=0.99,
            cash_buffer_weight=0.01,
            costs=costs
            or CostSpec(
                commission_bps=3.0,
                minimum_commission=5.0,
                sell_tax_bps=5.0,
                transfer_fee_bps=0.1,
                slippage_bps=5.0,
                rule_version="cn-equity-2025-01-01",
            ),
            risk=RiskSpec(),
            evaluation=EvaluationSpec(
                train_end=date(2025, 1, 2),
                validation_end=date(2025, 1, 3),
                test_end=date(2025, 1, 7),
                benchmark="000300.SH",
            ),
            ranking_field="momentum_20d",
            ranking_direction="descending",
            top_n=1,
        ),
        source=source,
        created_at=_T0,
    )
    compilation = compile_strategy_template(
        build,
        snapshot=snapshot,
        engine=EngineIdentitySpec(name=engine_name, commit=engine_commit),
        resource_limits=ResourceLimits(
            timeout_seconds=30,
            max_stdout_bytes=1_000_000,
            max_stderr_bytes=1_000_000,
            memory_bytes=536_870_912,
        ),
        random_seed=7,
        created_at=_T0,
    )
    return snapshot_path, snapshot, compilation


def _worker_payload(compilation: Any, snapshot: Any) -> dict[str, Any]:
    return {
        "schema_version": "vibe.vnpy-event-path-request.v1",
        "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
    }


class _FakeRunner:
    def __init__(
        self,
        *,
        mutate_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = SimpleNamespace(
            engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT)
        )
        self.mutate_result = mutate_result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        try:
            result = dict(
                build_event_replay_handler(_boundary)(
                    kwargs["payload"],
                    {
                        "path": kwargs["snapshot_path"],
                        "sha256": kwargs["snapshot_sha256"],
                    },
                )
            )
            if self.mutate_result is not None:
                self.mutate_result(result)
            response = {"status": "ok", "error": None, "result": result}
        except WorkerError as exc:
            response = {
                "status": "error",
                "error": {"code": exc.code, "message": str(exc)},
                "result": None,
            }
        return SimpleNamespace(response=response)


def test_qe6_1_replays_exact_qe5_identities_through_event_engine(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation = _compilation(tmp_path)
    runner = _FakeRunner()

    replay = VnpyOracleAdapter(runner).replay_event_path(  # type: ignore[arg-type]
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
    )

    request = compilation.engine_request.payload
    assert replay.qe5_engine_request is request
    assert replay.worker.engine_request_sha256 == canonical_sha256(request)
    assert replay.worker.execution_plan_sha256 == compilation.plan.content_sha256
    assert replay.worker.snapshot_sha256 == compute_snapshot_sha256(snapshot_path)
    assert [event.kind for event in replay.worker.events] == [
        "engine_request",
        "execution_plan",
        "data_snapshot",
    ]
    assert runner.calls[0]["operation"] == "event_replay"
    assert runner.calls[0]["request_id"] == f"qe6:{request.request_id}"
    assert runner.calls[0]["memory_bytes"] == 536_870_912
    assert runner.calls[0]["max_open_files"] == 256


def test_qe6_1_worker_rejects_plan_and_snapshot_identity_drift(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation = _compilation(tmp_path)
    payload = _worker_payload(compilation, snapshot)
    snapshot_wire = {
        "path": str(snapshot_path),
        "sha256": compute_snapshot_sha256(snapshot_path),
    }
    changed_plan = deepcopy(payload)
    changed_plan["execution_plan"]["strategy"]["title"] = "tampered"
    with pytest.raises(WorkerError) as plan_error:
        build_event_replay_handler(_boundary)(changed_plan, snapshot_wire)
    assert plan_error.value.code == "PLAN_IDENTITY_MISMATCH"

    changed_snapshot = deepcopy(payload)
    changed_snapshot["data_snapshot_ref"]["snapshot_sha256"] = "0" * 64
    with pytest.raises(WorkerError) as snapshot_error:
        build_event_replay_handler(_boundary)(changed_snapshot, snapshot_wire)
    assert snapshot_error.value.code == "SNAPSHOT_HASH_MISMATCH"


def test_qe6_1_rejects_non_qe5_request_and_wrong_vnpy_provenance(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot, compilation = _compilation(
        tmp_path,
        engine_name="vnpy",
        engine_commit=VNPY_ENGINE_COMMIT,
    )
    with pytest.raises(ValueError, match="exact audited QE5"):
        VnpyOracleAdapter(_FakeRunner()).replay_event_path(  # type: ignore[arg-type]
            compilation=compilation,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
        )

    snapshot_path, snapshot, compilation = _compilation(tmp_path / "provenance")

    def mutate(result: dict[str, Any]) -> None:
        result["engine_version"] = "0.0.invalid"

    with pytest.raises(ValueError, match="provenance"):
        VnpyOracleAdapter(  # type: ignore[arg-type]
            _FakeRunner(mutate_result=mutate)
        ).replay_event_path(
            compilation=compilation,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
        )


@pytest.mark.integration
def test_qe6_1_real_pinned_vnpy_event_engine(tmp_path: Path) -> None:
    python_text = os.environ.get("VIBE_QE0_VNPY_PYTHON")
    if not python_text:
        pytest.skip("set VIBE_QE0_VNPY_PYTHON to run pinned vn.py QE6-1")
    snapshot_path, snapshot, compilation = _compilation(tmp_path)
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("vnpy", VNPY_ENGINE_COMMIT),
            python=Path(python_text).absolute(),
            script=(WORKER_DIR / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_DIR.resolve(),
    )

    replay = VnpyOracleAdapter(runner).replay_event_path(
        compilation=compilation,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
    )

    assert replay.worker.engine_version == VNPY_ENGINE_VERSION
    assert replay.worker.source_sha256 == VNPY_SOURCE_SHA256
