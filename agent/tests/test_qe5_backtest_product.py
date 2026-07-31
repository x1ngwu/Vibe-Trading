"""QE5-5 Agent/API/SSE/history presentation over the governed runtime."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_qe5_backtest_run import _run_chain, _store_parents
from tests.test_qe5_quantaxis_backtest import _WorkerRunner
from src.agent.context import _SYSTEM_PROMPT
from src.quant_engine import (
    BacktestJobStore,
    BacktestProductService,
    BacktestRunStore,
    BacktestRuntime,
    QuantaxisAdapter,
)
from src.quant_engine.product import persist_backtest_visualization
from src.research.store import ResearchStore
from src.session.service import (
    _persisted_backtest_history,
    load_visualization_specs,
)
from src.tools import build_registry
from src.tools.strategy_backtest_tool import StrategyBacktestTool


def _wait(service: BacktestProductService, session_id: str, job_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = service.payload(session_id=session_id, job_id=job_id)
        if payload is not None and payload.status in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("backtest product job did not finish")


def _service(tmp_path: Path):
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research = ResearchStore(tmp_path / "research")
    _store_parents(
        research,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    source = tmp_path / "fixture" / "qe5-backtest-snapshot.json"
    target = snapshot_root / f"{snapshot.payload.snapshot_sha256}.json"
    target.write_bytes(source.read_bytes())
    runs = BacktestRunStore(tmp_path / "runs", research_store=research)
    jobs = BacktestJobStore(tmp_path / "jobs")
    runtime = BacktestRuntime(
        job_store=jobs,
        run_store=runs,
        max_concurrent=1,
        max_queued=2,
    )
    service = BacktestProductService(
        research_store=research,
        version_db_path=tmp_path / "fixture" / "versions.db",
        snapshot_root=snapshot_root,
        run_store=runs,
        job_store=jobs,
        runtime=runtime,
        adapter=QuantaxisAdapter(_WorkerRunner()),  # type: ignore[arg-type]
        resource_limits=compilation.plan.resource_limits,
        random_seed=compilation.plan.random_seed,
    )
    return service, prepared, version


def test_qe5_5_agent_tool_is_strict_session_injected_and_not_live() -> None:
    registry = build_registry(session_id="qe5-session", include_shell_tools=False)
    tool = registry.get("run_strategy_backtest")

    assert isinstance(tool, StrategyBacktestTool)
    assert set(tool.parameters["properties"]) == {
        "strategy_version_id",
        "idempotency_key",
    }
    assert tool.parameters["additionalProperties"] is False
    assert "broker" in tool.description
    assert "run_strategy_backtest" in _SYSTEM_PROMPT
    assert "governed" in _SYSTEM_PROMPT


def test_qe5_5_submit_idempotency_result_projection_and_history_recovery(
    tmp_path: Path,
) -> None:
    service, prepared, version = _service(tmp_path)
    try:
        first, spec = service.submit(
            session_id=version.stream_id,
            strategy_version_id=version.version_id,
            idempotency_key="product-run-1",
            initial_cash_fen=1_000_000,
        )
        completed = _wait(service, version.stream_id, first.job.job_id)
        retry, retry_spec = service.submit(
            session_id=version.stream_id,
            strategy_version_id=version.version_id,
            idempotency_key="product-run-1",
            initial_cash_fen=1_000_000,
        )

        assert first.created is True
        assert retry.created is False
        assert retry.job.job_id == first.job.job_id
        assert retry_spec == spec
        assert completed.run_id == prepared.record.run_id
        assert completed.metrics is not None
        assert completed.metrics["trade_count"] == 3
        assert len(completed.equity) == 4
        assert len(completed.trades) == 3
        assert completed.snapshot_sha256 == prepared.record.provenance.snapshot_sha256
        assert service.payload(
            session_id="another-session",
            job_id=first.job.job_id,
        ) is None

        run_dir = tmp_path / "chat-run"
        persist_backtest_visualization(run_dir, spec)
        loaded = load_visualization_specs(run_dir)
        assert loaded == [spec.model_dump(mode="json")]
        history = _persisted_backtest_history(
            {"visualizations": loaded},
            session_id=version.stream_id,
        )
        assert first.job.job_id in history
        assert _persisted_backtest_history(
            {"visualizations": loaded},
            session_id="another-session",
        ) == ""
    finally:
        service.runtime.close()


def test_qe5_5_agent_tool_persists_job_card_and_completes_same_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service, _prepared, version = _service(tmp_path)
    run_dir = tmp_path / "agent-run"
    monkeypatch.setattr(
        "src.tools.strategy_backtest_tool.safe_run_dir",
        lambda _value: run_dir,
    )
    try:
        result = json.loads(
            StrategyBacktestTool(
                default_session_id=version.stream_id,
                service=service,
            ).execute(
                strategy_version_id=version.version_id,
                idempotency_key="agent-product-1",
                run_dir="agent-run",
            )
        )
        payload = _wait(service, version.stream_id, result["job_id"])

        assert result["status"] in {"queued", "running", "completed"}
        assert result["initial_cash_fen"] == 100_000_000
        assert result["live_trading"] is False
        assert payload.status == "completed"
        assert payload.strategy_version_id == version.version_id
        assert load_visualization_specs(run_dir) == result["visualizations"]
    finally:
        service.runtime.close()


def test_qe5_5_authenticated_api_status_cancel_scope_compare_and_sse(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service, _prepared, version = _service(tmp_path)
    first, _spec = service.submit(
        session_id=version.stream_id,
        strategy_version_id=version.version_id,
        idempotency_key="api-product-1",
        initial_cash_fen=1_000_000,
    )
    _wait(service, version.stream_id, first.job.job_id)

    import api_server
    import src.api.backtest_routes as routes

    class _Sessions:
        @staticmethod
        def get_session(session_id: str):
            return object() if session_id in {version.stream_id, "other-session"} else None

    monkeypatch.setattr(routes, "_backtest_service", service)
    monkeypatch.setattr(api_server, "_get_session_service", lambda: _Sessions())
    client = TestClient(api_server.app)
    try:
        detail = client.get(
            f"/sessions/{version.stream_id}/backtests/{first.job.job_id}"
        )
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"
        assert client.get(
            f"/sessions/other-session/backtests/{first.job.job_id}"
        ).status_code == 404

        listed = client.get(f"/sessions/{version.stream_id}/backtests").json()
        assert [item["job_id"] for item in listed] == [first.job.job_id]
        comparison = client.post(
            f"/sessions/{version.stream_id}/backtests/compare",
            json={
                "left_job_id": first.job.job_id,
                "right_job_id": first.job.job_id,
            },
        )
        assert comparison.status_code == 200
        assert all(item["delta"] == 0 for item in comparison.json()["deltas"] if item["delta"] is not None)

        with client.stream(
            "GET",
            f"/sessions/{version.stream_id}/backtests/{first.job.job_id}/events",
        ) as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert "event: backtest" in body
        assert '"status":"completed"' in body
        assert "event: done" in body

        cancelled = client.post(
            f"/sessions/{version.stream_id}/backtests/{first.job.job_id}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "completed"
    finally:
        service.runtime.close()
