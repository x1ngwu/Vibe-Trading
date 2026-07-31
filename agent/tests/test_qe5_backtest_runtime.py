"""QE5-4 persistent queue, cancellation, backpressure, and recovery tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import sys
import threading
import time

import pytest

from tests.test_qe5_backtest_run import _run_chain, _store_parents
from src.quant_engine import (
    BacktestBackpressureError,
    BacktestJobDiagnostic,
    BacktestJobIntegrityError,
    BacktestJobStore,
    BacktestRuntime,
    BacktestSchedulerLeaseError,
    BacktestRunStore,
    EngineIdentity,
    PersistedBacktestRun,
    WorkerConfig,
    WorkerExecutionError,
    WorkerRunner,
    backup_backtest_runtime,
)
from src.research.store import ResearchStore


_T0 = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)
_AGENT_ROOT = Path(__file__).resolve().parents[1]
_FAKE_WORKER = (_AGENT_ROOT / "tests" / "fixtures" / "fake_quant_worker.py").resolve()
_COMMON_RUNTIME = (_AGENT_ROOT / "engine_workers" / "common").resolve()


class _MemoryRunStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def put(self, prepared, *, idempotency_key: str) -> PersistedBacktestRun:
        self.calls.append(idempotency_key)
        return PersistedBacktestRun(
            record=prepared.record,
            object=prepared.object,
            created=True,
        )

    def get_by_idempotency_key(self, *, owner_scope: str, idempotency_key: str):
        return None


def _wait_for_job(
    runtime: BacktestRuntime,
    job_id: str,
    *,
    owner_scope: str,
    status: str | None = None,
    timeout: float = 3.0,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runtime.get(job_id, owner_scope=owner_scope)
        if job is not None and (status is None or job.status == status):
            return job
        time.sleep(0.01)
    job = runtime.get(job_id, owner_scope=owner_scope)
    pytest.fail(f"job did not reach {status}; last={job}")


def test_qe5_4_runtime_persists_success_and_idempotent_submission(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path / "fixture")
    owner_scope = prepared.record.owner_scope
    jobs = BacktestJobStore(tmp_path / "jobs")
    runs = _MemoryRunStore()
    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=jobs,
        run_store=runs,
        max_concurrent=1,
        max_queued=2,
    )
    try:
        first = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="runtime-success",
            request_sha256=prepared.record.provenance.backtest_input_sha256,
            execute=lambda _cancel: prepared,
        )
        completed = _wait_for_job(
            runtime,
            first.job.job_id,
            owner_scope=owner_scope,
            status="completed",
        )
        retry = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="runtime-success",
            request_sha256=prepared.record.provenance.backtest_input_sha256,
            execute=lambda _cancel: pytest.fail("idempotent retry executed"),
        )
    finally:
        runtime.close()

    assert first.created is True
    assert retry.created is False
    assert retry.job == completed
    assert completed.run_id == prepared.record.run_id
    assert completed.attempts == 1
    assert runs.calls == ["runtime-success"]
    assert jobs.get(completed.job_id, owner_scope="household:other") is None


def test_qe5_4_queue_backpressure_and_queued_cancel_are_bounded(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path / "fixture")
    owner_scope = prepared.record.owner_scope
    request_sha256 = prepared.record.provenance.backtest_input_sha256
    jobs = BacktestJobStore(tmp_path / "jobs")
    runs = _MemoryRunStore()
    first_started = threading.Event()
    release_first = threading.Event()
    second_called = threading.Event()

    def first_execute(cancel: threading.Event):
        first_started.set()
        assert release_first.wait(timeout=2)
        assert not cancel.is_set()
        return prepared

    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=jobs,
        run_store=runs,
        max_concurrent=1,
        max_queued=1,
    )
    try:
        first = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="bounded-1",
            request_sha256=request_sha256,
            execute=first_execute,
        )
        assert first_started.wait(timeout=2)
        second = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="bounded-2",
            request_sha256=request_sha256,
            execute=lambda _cancel: second_called.set() or prepared,
        )
        with pytest.raises(BacktestBackpressureError, match="queue is full"):
            runtime.submit(
                owner_scope=owner_scope,
                idempotency_key="bounded-3",
                request_sha256=request_sha256,
                execute=lambda _cancel: prepared,
            )
        cancelled = runtime.cancel(second.job.job_id, owner_scope=owner_scope)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        snapshot = runtime.queue_snapshot()
        assert snapshot.running == 1
        release_first.set()
        _wait_for_job(
            runtime,
            first.job.job_id,
            owner_scope=owner_scope,
            status="completed",
        )
    finally:
        release_first.set()
        runtime.close()

    assert second_called.is_set() is False
    assert jobs.get(second.job.job_id, owner_scope=owner_scope).status == "cancelled"  # type: ignore[union-attr]
    assert jobs.get_by_idempotency_key(
        owner_scope=owner_scope,
        idempotency_key="bounded-3",
    ) is None


def test_qe5_4_running_cancel_propagates_and_keeps_diagnostic(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path / "fixture")
    owner_scope = prepared.record.owner_scope
    jobs = BacktestJobStore(tmp_path / "jobs")
    started = threading.Event()

    def execute(cancel: threading.Event):
        started.set()
        assert cancel.wait(timeout=2)
        raise WorkerExecutionError(
            "WORKER_CANCELLED",
            "worker was cancelled",
            stderr="bounded worker diagnostic",
        )

    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=jobs,
        run_store=_MemoryRunStore(),
        max_concurrent=1,
        max_queued=1,
    )
    try:
        submitted = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="cancel-running",
            request_sha256="4" * 64,
            execute=execute,
        )
        assert started.wait(timeout=2)
        requested = runtime.cancel(submitted.job.job_id, owner_scope=owner_scope)
        assert requested is not None
        terminal = _wait_for_job(
            runtime,
            submitted.job.job_id,
            owner_scope=owner_scope,
            status="cancelled",
        )
    finally:
        runtime.close()

    assert terminal.cancel_requested is True
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.code == "WORKER_CANCELLED"
    assert terminal.diagnostic.stderr == "bounded worker diagnostic"


def test_qe5_4_timeout_failure_reaches_terminal_state_with_diagnostic(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path / "fixture")
    owner_scope = prepared.record.owner_scope
    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=BacktestJobStore(tmp_path / "jobs"),
        run_store=_MemoryRunStore(),
        max_concurrent=1,
        max_queued=1,
    )
    try:
        submitted = runtime.submit(
            owner_scope=owner_scope,
            idempotency_key="timeout-failure",
            request_sha256="7" * 64,
            execute=lambda _cancel: (_ for _ in ()).throw(
                WorkerExecutionError(
                    "WORKER_TIMEOUT",
                    "worker exceeded timeout",
                    stderr="last bounded stderr",
                )
            ),
        )
        terminal = _wait_for_job(
            runtime,
            submitted.job.job_id,
            owner_scope=owner_scope,
            status="failed",
        )
    finally:
        runtime.close()

    assert terminal.diagnostic is not None
    assert terminal.diagnostic.code == "WORKER_TIMEOUT"
    assert terminal.diagnostic.stderr == "last bounded stderr"


def test_qe5_4_job_identity_must_match_prepared_run_before_persistence(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path / "fixture")
    runs = _MemoryRunStore()
    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=BacktestJobStore(tmp_path / "jobs"),
        run_store=runs,
        max_concurrent=1,
        max_queued=1,
    )
    try:
        submitted = runtime.submit(
            owner_scope=prepared.record.owner_scope,
            idempotency_key="identity-mismatch",
            request_sha256="0" * 64,
            execute=lambda _cancel: prepared,
        )
        terminal = _wait_for_job(
            runtime,
            submitted.job.job_id,
            owner_scope=prepared.record.owner_scope,
            status="failed",
        )
    finally:
        runtime.close()

    assert terminal.diagnostic is not None
    assert terminal.diagnostic.code == "BACKTEST_FAILED"
    assert "persistent job identity" in terminal.diagnostic.message
    assert runs.calls == []


def test_qe5_4_runtime_cancel_terminates_real_worker_process_group(
    tmp_path: Path,
) -> None:
    snapshot_root = (tmp_path / "snapshots").resolve()
    snapshot_root.mkdir()
    runner = WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity("fake", "a" * 40),
            python=Path(sys.executable).resolve(),
            script=_FAKE_WORKER,
            snapshot_root=snapshot_root,
        ),
        common_runtime=_COMMON_RUNTIME,
    )
    pid_path = tmp_path / "child.pid"
    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=BacktestJobStore(tmp_path / "jobs"),
        run_store=_MemoryRunStore(),
        max_concurrent=1,
        max_queued=1,
    )

    def execute(cancel: threading.Event):
        return runner.run(
            request_id="qe5-runtime-process-group",
            operation="spawn_and_sleep",
            payload={"pid_file": str(pid_path)},
            timeout_seconds=5,
            cancel_event=cancel,
        )

    try:
        submitted = runtime.submit(
            owner_scope="household:v1",
            idempotency_key="cancel-process-group",
            request_sha256="8" * 64,
            execute=execute,  # type: ignore[arg-type]
        )
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        runtime.cancel(submitted.job.job_id, owner_scope="household:v1")
        terminal = _wait_for_job(
            runtime,
            submitted.job.job_id,
            owner_scope="household:v1",
            status="cancelled",
        )
    finally:
        runtime.close()

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        process_state = Path(f"/proc/{child_pid}/stat").read_text(
            encoding="utf-8"
        ).split()[2]
        if process_state == "Z":
            break
        time.sleep(0.02)
    if Path(f"/proc/{child_pid}").exists():
        assert Path(f"/proc/{child_pid}/stat").read_text(
            encoding="utf-8"
        ).split()[2] == "Z"
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.code == "WORKER_CANCELLED"


def test_qe5_4_runtime_integrates_with_atomic_backtest_persistence(
    tmp_path: Path,
) -> None:
    (
        prepared,
        _result,
        snapshot,
        compilation,
        version,
        _head,
        _card,
        _receipt,
    ) = _run_chain(tmp_path / "fixture")
    research = ResearchStore(tmp_path / "research")
    _store_parents(
        research,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    runs = BacktestRunStore(tmp_path / "runs", research_store=research)
    jobs = BacktestJobStore(tmp_path / "jobs")
    with BacktestRuntime(
        job_store=jobs,
        run_store=runs,
        max_concurrent=1,
        max_queued=1,
    ) as runtime:
        submitted = runtime.submit(
            owner_scope=version.owner_scope,
            idempotency_key="persist-real-run",
            request_sha256=prepared.record.provenance.backtest_input_sha256,
            execute=lambda _cancel: prepared,
        )
        completed = _wait_for_job(
            runtime,
            submitted.job.job_id,
            owner_scope=version.owner_scope,
            status="completed",
        )

    restored = runs.get(completed.run_id, owner_scope=version.owner_scope)  # type: ignore[arg-type]
    assert restored is not None
    assert restored.record == prepared.record

    backup = backup_backtest_runtime(
        tmp_path / "runtime-backup",
        job_store=jobs,
        run_store=runs,
    )
    assert backup.job_count == 1
    assert backup.storage.run_count == 1
    restored_jobs = BacktestJobStore(backup.destination / "jobs")
    assert restored_jobs.get(
        submitted.job.job_id,
        owner_scope=version.owner_scope,
    ) == completed
    restored_research = ResearchStore(backup.destination / "storage" / "research")
    restored_runs = BacktestRunStore(
        backup.destination / "storage" / "backtests",
        research_store=restored_research,
    )
    assert restored_runs.get(
        completed.run_id,  # type: ignore[arg-type]
        owner_scope=version.owner_scope,
    ) is not None


def test_qe5_4_restart_reconciliation_and_scheduler_lease(
    tmp_path: Path,
) -> None:
    jobs = BacktestJobStore(tmp_path / "jobs")
    queued = jobs.submit(
        owner_scope="household:v1",
        idempotency_key="stale-queued",
        request_sha256="5" * 64,
        submitted_at=_T0,
    ).job
    running = jobs.submit(
        owner_scope="household:v1",
        idempotency_key="stale-running",
        request_sha256="6" * 64,
        submitted_at=_T0,
    ).job
    assert jobs.claim(running.job_id, started_at=_T0 + timedelta(seconds=1))

    runtime = BacktestRuntime(  # type: ignore[arg-type]
        job_store=jobs,
        run_store=_MemoryRunStore(),
        max_concurrent=1,
        max_queued=1,
    )
    try:
        for job_id in (queued.job_id, running.job_id):
            recovered = jobs.get(job_id, owner_scope="household:v1")
            assert recovered is not None
            assert recovered.status == "failed"
            assert recovered.diagnostic is not None
            assert recovered.diagnostic.code == "RUNTIME_RESTARTED"
        with pytest.raises(BacktestSchedulerLeaseError, match="another"):
            with jobs.scheduler_lease():
                pass
    finally:
        runtime.close()


def test_qe5_4_restart_reconciles_committed_run_as_completed(
    tmp_path: Path,
) -> None:
    (
        prepared,
        _result,
        snapshot,
        compilation,
        version,
        _head,
        _card,
        _receipt,
    ) = _run_chain(tmp_path / "fixture")
    research = ResearchStore(tmp_path / "research")
    _store_parents(
        research,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    runs = BacktestRunStore(tmp_path / "runs", research_store=research)
    jobs = BacktestJobStore(tmp_path / "jobs")
    submitted = jobs.submit(
        owner_scope=version.owner_scope,
        idempotency_key="crash-after-run-commit",
        request_sha256=prepared.record.provenance.backtest_input_sha256,
    ).job
    assert jobs.claim(submitted.job_id) is not None
    persisted = runs.put(
        prepared,
        idempotency_key="crash-after-run-commit",
    )

    runtime = BacktestRuntime(
        job_store=jobs,
        run_store=runs,
        max_concurrent=1,
        max_queued=1,
    )
    try:
        recovered = jobs.get(submitted.job_id, owner_scope=version.owner_scope)
    finally:
        runtime.close()

    assert recovered is not None
    assert recovered.status == "completed"
    assert recovered.run_id == persisted.record.run_id
    assert recovered.diagnostic is None


def test_qe5_4_job_backup_retention_and_permissions(tmp_path: Path) -> None:
    jobs = BacktestJobStore(tmp_path / "jobs")
    for index in range(3):
        job = jobs.submit(
            owner_scope="household:v1",
            idempotency_key=f"terminal-{index}",
            request_sha256=f"{index + 1}" * 64,
            submitted_at=_T0 + timedelta(minutes=index),
        ).job
        assert jobs.claim(job.job_id, started_at=_T0 + timedelta(minutes=index))
        jobs.fail(
            job.job_id,
            diagnostic=BacktestJobDiagnostic(
                code="FIXTURE_FAILURE",
                message=f"failure {index}",
            ),
            finished_at=_T0 + timedelta(minutes=index, seconds=1),
        )

    backup = tmp_path / "backup"
    assert jobs.backup_to(backup) == 3
    restored = BacktestJobStore(backup)
    assert len(restored.list(owner_scope="household:v1")) == 3
    assert stat.S_IMODE(jobs.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(jobs.database_path.stat().st_mode) == 0o600

    assert jobs.prune_terminal(
        owner_scope="household:v1",
        keep_last=1,
        finished_before=_T0 + timedelta(hours=1),
    ) == 2
    retained = jobs.list(owner_scope="household:v1")
    assert len(retained) == 1
    assert retained[0].idempotency_key == "terminal-2"

    linked_root = tmp_path / "linked-jobs"
    linked_root.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    (linked_root / "backtest-jobs.db").symlink_to(outside)
    with pytest.raises(BacktestJobIntegrityError, match="symlink"):
        BacktestJobStore(linked_root)
