"""QE5-4 bounded, persistent orchestration for isolated backtest workers."""

from __future__ import annotations

import fcntl
import os
import queue
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import canonical_json, canonical_sha256

from .backtest_run import (
    BacktestBackupResult,
    BacktestRunStore,
    PersistedBacktestRun,
    PreparedBacktestRun,
)
from .runner import WorkerExecutionError


_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_JOB_ID_PATTERN = r"^backtest-job:[0-9a-f]{64}$"
_OWNER_PATTERN = r"^[a-z][a-z0-9._:-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class BacktestRuntimeError(RuntimeError):
    """Base error for persistent QE5 runtime governance."""


class BacktestBackpressureError(BacktestRuntimeError):
    """The bounded pending queue has no capacity for another semantic run."""


class BacktestJobConflictError(BacktestRuntimeError):
    """An idempotency key was reused for a different worker request."""


class BacktestJobIntegrityError(BacktestRuntimeError):
    """Persistent job state or storage layout is inconsistent."""


class BacktestSchedulerLeaseError(BacktestRuntimeError):
    """Another scheduler already owns this job store."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestJobDiagnostic(_StrictModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    stderr: str = Field(default="", max_length=16_384)


class BacktestJob(_StrictModel):
    job_id: str = Field(pattern=_JOB_ID_PATTERN)
    owner_scope: str = Field(pattern=_OWNER_PATTERN)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    submitted_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    run_id: str | None = Field(default=None, pattern=r"^backtest-record:[0-9a-f]{64}$")
    diagnostic: BacktestJobDiagnostic | None = None
    cancel_requested: bool = False
    attempts: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "BacktestJob":
        if self.status == "queued" and (
            self.started_at is not None
            or self.finished_at is not None
            or self.run_id is not None
            or self.diagnostic is not None
        ):
            raise ValueError("queued job has terminal or running fields")
        if self.status == "running" and (
            self.started_at is None
            or self.finished_at is not None
            or self.run_id is not None
            or self.diagnostic is not None
        ):
            raise ValueError("running job has invalid lifecycle fields")
        if self.status in _TERMINAL_STATES and self.finished_at is None:
            raise ValueError("terminal job requires finished_at")
        if self.status == "completed" and (
            self.run_id is None or self.diagnostic is not None
        ):
            raise ValueError("completed job requires only a run_id")
        if self.status in {"failed", "cancelled"} and (
            self.run_id is not None or self.diagnostic is None
        ):
            raise ValueError("failed/cancelled job requires only a diagnostic")
        return self


@dataclass(frozen=True)
class BacktestSubmitResult:
    job: BacktestJob
    created: bool


@dataclass(frozen=True)
class BacktestQueueSnapshot:
    max_concurrent: int
    max_queued: int
    running: int
    queued: int


@dataclass(frozen=True)
class BacktestRuntimeBackupResult:
    destination: Path
    job_count: int
    storage: BacktestBackupResult
    manifest_sha256: str


BacktestExecution = Callable[[threading.Event], PreparedBacktestRun]


class BacktestJobStore:
    """Mutable SQLite/WAL lifecycle state with strict owner/idempotency scope."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.database_path = self.root / "backtest-jobs.db"
        self.lock_path = self.root / ".jobs.lock"
        self.scheduler_lock_path = self.root / ".scheduler.lock"
        self._prepare()
        self._initialize_database()

    def submit(
        self,
        *,
        owner_scope: str,
        idempotency_key: str,
        request_sha256: str,
        submitted_at: datetime | None = None,
    ) -> BacktestSubmitResult:
        if not re.fullmatch(_OWNER_PATTERN, owner_scope):
            raise ValueError("owner_scope is invalid")
        if not re.fullmatch(_IDEMPOTENCY_PATTERN, idempotency_key):
            raise ValueError("idempotency_key is invalid")
        if not re.fullmatch(_SHA256_PATTERN, request_sha256):
            raise ValueError("request_sha256 is invalid")
        identity = {
            "owner_scope": owner_scope,
            "idempotency_key": idempotency_key,
        }
        job_id = f"backtest-job:{canonical_sha256(identity)}"
        timestamp = self._utc(submitted_at)
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE owner_scope=? AND idempotency_key=?
                    """,
                    (owner_scope, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_job(row)
                    if existing.request_sha256 != request_sha256:
                        raise BacktestJobConflictError(
                            "idempotency key was used for another backtest request"
                        )
                    connection.commit()
                    return BacktestSubmitResult(job=existing, created=False)
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, owner_scope, idempotency_key, request_sha256,
                        status, submitted_at, started_at, finished_at, run_id,
                        diagnostic_code, diagnostic_message, diagnostic_stderr,
                        cancel_requested, attempts
                    ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, NULL,
                              NULL, NULL, NULL, 0, 0)
                    """,
                    (
                        job_id,
                        owner_scope,
                        idempotency_key,
                        request_sha256,
                        timestamp.isoformat(),
                    ),
                )
                connection.commit()
        job = self.get(job_id, owner_scope=owner_scope)
        if job is None:
            raise BacktestJobIntegrityError("new job disappeared after commit")
        return BacktestSubmitResult(job=job, created=True)

    def get(self, job_id: str, *, owner_scope: str) -> BacktestJob | None:
        self._validate_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? AND owner_scope=?",
                (job_id, owner_scope),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def get_by_idempotency_key(
        self,
        *,
        owner_scope: str,
        idempotency_key: str,
    ) -> BacktestJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE owner_scope=? AND idempotency_key=?
                """,
                (owner_scope, idempotency_key),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list(
        self,
        *,
        owner_scope: str,
        limit: int = 100,
    ) -> tuple[BacktestJob, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE owner_scope=?
                ORDER BY submitted_at DESC, job_id ASC LIMIT ?
                """,
                (owner_scope, limit),
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def claim(self, job_id: str, *, started_at: datetime | None = None) -> BacktestJob | None:
        timestamp = self._utc(started_at)
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status='running', started_at=?, attempts=attempts + 1
                    WHERE job_id=? AND status='queued' AND cancel_requested=0
                    """,
                    (timestamp.isoformat(), job_id),
                ).rowcount
                connection.commit()
        if not updated:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise BacktestJobIntegrityError("claimed job disappeared")
        return self._row_to_job(row)

    def request_cancel(
        self,
        job_id: str,
        *,
        owner_scope: str,
        requested_at: datetime | None = None,
    ) -> BacktestJob | None:
        timestamp = self._utc(requested_at)
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=? AND owner_scope=?",
                    (job_id, owner_scope),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                job = self._row_to_job(row)
                if job.status in _TERMINAL_STATES:
                    connection.commit()
                    return job
                if job.status == "queued":
                    diagnostic = BacktestJobDiagnostic(
                        code="JOB_CANCELLED",
                        message="backtest was cancelled before worker start",
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status='cancelled', finished_at=?,
                            diagnostic_code=?, diagnostic_message=?,
                            diagnostic_stderr='', cancel_requested=1
                        WHERE job_id=? AND status='queued'
                        """,
                        (
                            timestamp.isoformat(),
                            diagnostic.code,
                            diagnostic.message,
                            job_id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET cancel_requested=1 WHERE job_id=?",
                        (job_id,),
                    )
                connection.commit()
        result = self.get(job_id, owner_scope=owner_scope)
        if result is None:
            raise BacktestJobIntegrityError("cancelled job disappeared")
        return result

    def complete(
        self,
        job_id: str,
        *,
        run_id: str,
        finished_at: datetime | None = None,
    ) -> BacktestJob:
        return self._finish(
            job_id,
            status="completed",
            run_id=run_id,
            diagnostic=None,
            finished_at=finished_at,
        )

    def fail(
        self,
        job_id: str,
        *,
        diagnostic: BacktestJobDiagnostic,
        finished_at: datetime | None = None,
    ) -> BacktestJob:
        return self._finish(
            job_id,
            status="failed",
            run_id=None,
            diagnostic=diagnostic,
            finished_at=finished_at,
        )

    def cancel_running(
        self,
        job_id: str,
        *,
        diagnostic: BacktestJobDiagnostic,
        finished_at: datetime | None = None,
    ) -> BacktestJob:
        return self._finish(
            job_id,
            status="cancelled",
            run_id=None,
            diagnostic=diagnostic,
            finished_at=finished_at,
        )

    def reconcile_incomplete(self, *, reconciled_at: datetime | None = None) -> int:
        """Fail stale queued/running rows after an unclean scheduler restart."""

        timestamp = self._utc(reconciled_at)
        diagnostic = BacktestJobDiagnostic(
            code="RUNTIME_RESTARTED",
            message="scheduler restarted before the backtest reached a terminal state",
        )
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET status='failed', finished_at=?,
                        diagnostic_code=?, diagnostic_message=?,
                        diagnostic_stderr='', cancel_requested=1
                    WHERE status IN ('queued', 'running')
                    """,
                    (
                        timestamp.isoformat(),
                        diagnostic.code,
                        diagnostic.message,
                    ),
                ).rowcount
                connection.commit()
        return changed

    def list_incomplete(self) -> tuple[BacktestJob, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY submitted_at ASC, job_id ASC
                """
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def reconcile_job(
        self,
        job_id: str,
        *,
        run_id: str | None,
        reconciled_at: datetime | None = None,
    ) -> BacktestJob:
        """Close one stale job as completed when its durable run committed."""

        timestamp = self._utc(reconciled_at)
        completed = run_id is not None
        diagnostic = (
            None
            if completed
            else BacktestJobDiagnostic(
                code="RUNTIME_RESTARTED",
                message=(
                    "scheduler restarted before the backtest reached "
                    "a terminal state"
                ),
            )
        )
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET status=?, finished_at=?, run_id=?,
                        diagnostic_code=?, diagnostic_message=?,
                        diagnostic_stderr=?,
                        cancel_requested=CASE WHEN ?='failed' THEN 1
                                              ELSE cancel_requested END
                    WHERE job_id=? AND status IN ('queued', 'running')
                    """,
                    (
                        "completed" if completed else "failed",
                        timestamp.isoformat(),
                        run_id,
                        diagnostic.code if diagnostic else None,
                        diagnostic.message if diagnostic else None,
                        diagnostic.stderr if diagnostic else None,
                        "completed" if completed else "failed",
                        job_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise BacktestJobIntegrityError(
                        "cannot reconcile a terminal or missing backtest job"
                    )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
        if row is None:
            raise BacktestJobIntegrityError("reconciled job disappeared")
        return self._row_to_job(row)

    def prune_terminal(
        self,
        *,
        owner_scope: str,
        keep_last: int,
        finished_before: datetime,
    ) -> int:
        """Prune old terminal job metadata without touching immutable run results."""

        if keep_last < 0:
            raise ValueError("keep_last must be non-negative")
        cutoff = self._utc(finished_before).isoformat()
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """
                    SELECT job_id FROM jobs
                    WHERE owner_scope=? AND status IN ('completed', 'failed', 'cancelled')
                    ORDER BY finished_at DESC, job_id ASC
                    """,
                    (owner_scope,),
                ).fetchall()
                retained = {row["job_id"] for row in rows[:keep_last]}
                candidates = [
                    row["job_id"]
                    for row in rows[keep_last:]
                    if connection.execute(
                        "SELECT finished_at < ? FROM jobs WHERE job_id=?",
                        (cutoff, row["job_id"]),
                    ).fetchone()[0]
                ]
                candidates = [item for item in candidates if item not in retained]
                connection.executemany(
                    "DELETE FROM jobs WHERE job_id=?",
                    ((job_id,) for job_id in candidates),
                )
                connection.commit()
        return len(candidates)

    def backup_to(self, destination: Path) -> int:
        destination = Path(destination)
        self._validate_backup_destination(destination)
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        target = destination / self.database_path.name
        with self._exclusive_lock():
            with self._connect() as source:
                with sqlite3.connect(target) as backup:
                    source.backup(backup)
                    backup.commit()
            os.chmod(target, 0o600)
            with self._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        restored = BacktestJobStore(destination)
        with restored._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY job_id"
            ).fetchall()
        for row in rows:
            restored._row_to_job(row)
        restored_count = len(rows)
        if restored_count != count:
            raise BacktestJobIntegrityError("job backup did not preserve every row")
        return int(count)

    @contextmanager
    def scheduler_lease(self) -> Iterator[None]:
        descriptor = os.open(
            self.scheduler_lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BacktestJobIntegrityError(
                    "scheduler lease must be a regular file"
                )
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BacktestSchedulerLeaseError(
                    "another backtest scheduler owns this job store"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _finish(
        self,
        job_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        run_id: str | None,
        diagnostic: BacktestJobDiagnostic | None,
        finished_at: datetime | None,
    ) -> BacktestJob:
        timestamp = self._utc(finished_at)
        with self._exclusive_lock():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET status=?, finished_at=?, run_id=?,
                        diagnostic_code=?, diagnostic_message=?,
                        diagnostic_stderr=?,
                        cancel_requested=CASE WHEN ?='cancelled' THEN 1
                                              ELSE cancel_requested END
                    WHERE job_id=? AND status='running'
                    """,
                    (
                        status,
                        timestamp.isoformat(),
                        run_id,
                        diagnostic.code if diagnostic else None,
                        diagnostic.message if diagnostic else None,
                        diagnostic.stderr if diagnostic else None,
                        status,
                        job_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise BacktestJobIntegrityError(
                        f"cannot transition job from non-running state to {status}"
                    )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
        if row is None:
            raise BacktestJobIntegrityError("finished job disappeared")
        return self._row_to_job(row)

    def _prepare(self) -> None:
        if self.root.is_symlink():
            raise BacktestJobIntegrityError("job store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for path in (
            self.database_path,
            self.lock_path,
            self.scheduler_lock_path,
        ):
            if path.is_symlink():
                raise BacktestJobIntegrityError("job store file must not be a symlink")
            if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
                raise BacktestJobIntegrityError(
                    "job store path must be a regular file"
                )
        self._ensure_regular_file(self.database_path)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                    ),
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    run_id TEXT,
                    diagnostic_code TEXT,
                    diagnostic_message TEXT,
                    diagnostic_stderr TEXT,
                    cancel_requested INTEGER NOT NULL CHECK(cancel_requested IN (0, 1)),
                    attempts INTEGER NOT NULL CHECK(attempts >= 0),
                    UNIQUE(owner_scope, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_owner_submitted
                    ON jobs(owner_scope, submitted_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_submitted
                    ON jobs(status, submitted_at ASC);
                """
            )
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._ensure_regular_file(self.database_path)
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._secure_database_files()
            yield connection
        finally:
            connection.close()
            self._secure_database_files()

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BacktestJobIntegrityError("job lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _ensure_regular_file(path: Path) -> None:
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise BacktestJobIntegrityError(f"cannot open job store file: {path}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BacktestJobIntegrityError(
                    f"job store path must be a regular file: {path}"
                )
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _secure_database_files(self) -> None:
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.exists() or path.is_symlink():
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise BacktestJobIntegrityError(
                        f"job database path must be a regular file: {path}"
                    )
                os.chmod(path, 0o600)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> BacktestJob:
        try:
            diagnostic = (
                BacktestJobDiagnostic(
                    code=row["diagnostic_code"],
                    message=row["diagnostic_message"],
                    stderr=row["diagnostic_stderr"] or "",
                )
                if row["diagnostic_code"] is not None
                else None
            )
            return BacktestJob(
                job_id=row["job_id"],
                owner_scope=row["owner_scope"],
                idempotency_key=row["idempotency_key"],
                request_sha256=row["request_sha256"],
                status=row["status"],
                submitted_at=datetime.fromisoformat(row["submitted_at"]),
                started_at=(
                    datetime.fromisoformat(row["started_at"])
                    if row["started_at"] is not None
                    else None
                ),
                finished_at=(
                    datetime.fromisoformat(row["finished_at"])
                    if row["finished_at"] is not None
                    else None
                ),
                run_id=row["run_id"],
                diagnostic=diagnostic,
                cancel_requested=bool(row["cancel_requested"]),
                attempts=row["attempts"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BacktestJobIntegrityError("invalid persistent backtest job row") from exc

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        result = value or datetime.now(timezone.utc)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return result.astimezone(timezone.utc)

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not re.fullmatch(_JOB_ID_PATTERN, job_id):
            raise BacktestJobIntegrityError("invalid backtest job ID")

    def _validate_backup_destination(self, destination: Path) -> None:
        if destination.is_symlink():
            raise BacktestJobIntegrityError("backup destination must not be a symlink")
        source = self.root.resolve()
        target = destination.resolve(strict=False)
        if source == target or source in target.parents or target in source.parents:
            raise BacktestJobIntegrityError(
                "backup destination must be outside the live job store"
            )
        if destination.exists() and (
            not destination.is_dir() or any(destination.iterdir())
        ):
            raise BacktestJobIntegrityError("backup destination must be an empty directory")


@dataclass
class _QueuedExecution:
    job_id: str
    owner_scope: str
    idempotency_key: str
    request_sha256: str
    execute: BacktestExecution
    cancel_event: threading.Event


class BacktestRuntime:
    """One lease-owning bounded scheduler that persists every lifecycle edge."""

    def __init__(
        self,
        *,
        job_store: BacktestJobStore,
        run_store: BacktestRunStore,
        max_concurrent: int = 2,
        max_queued: int = 8,
    ) -> None:
        if max_concurrent <= 0 or max_queued <= 0:
            raise ValueError("runtime concurrency and queue limits must be positive")
        self.job_store = job_store
        self.run_store = run_store
        self.max_concurrent = max_concurrent
        self.max_queued = max_queued
        self._queue: queue.Queue[_QueuedExecution | None] = queue.Queue(
            maxsize=max_queued
        )
        self._submission_lock = threading.Lock()
        self._tasks_lock = threading.Lock()
        self._tasks: dict[str, _QueuedExecution] = {}
        self._closed = False
        self._lease = self.job_store.scheduler_lease()
        self._lease.__enter__()
        self._workers: tuple[threading.Thread, ...] = ()
        try:
            self._reconcile_startup()
            self._workers = tuple(
                threading.Thread(
                    target=self._worker_loop,
                    name=f"backtest-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(max_concurrent)
            )
            for worker in self._workers:
                worker.start()
        except BaseException:
            self._lease.__exit__(None, None, None)
            raise

    def submit(
        self,
        *,
        owner_scope: str,
        idempotency_key: str,
        request_sha256: str,
        execute: BacktestExecution,
    ) -> BacktestSubmitResult:
        with self._submission_lock:
            if self._closed:
                raise BacktestRuntimeError("backtest runtime is closed")
            existing = self.job_store.get_by_idempotency_key(
                owner_scope=owner_scope,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise BacktestJobConflictError(
                        "idempotency key was used for another backtest request"
                    )
                return BacktestSubmitResult(job=existing, created=False)
            if self._queue.full():
                raise BacktestBackpressureError("backtest queue is full")
            submitted = self.job_store.submit(
                owner_scope=owner_scope,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            task = _QueuedExecution(
                job_id=submitted.job.job_id,
                owner_scope=owner_scope,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                execute=execute,
                cancel_event=threading.Event(),
            )
            with self._tasks_lock:
                self._tasks[task.job_id] = task
            try:
                self._queue.put_nowait(task)
            except queue.Full as exc:  # guarded by _submission_lock
                raise BacktestJobIntegrityError(
                    "queue capacity changed during serialized submission"
                ) from exc
            return submitted

    def cancel(self, job_id: str, *, owner_scope: str) -> BacktestJob | None:
        job = self.job_store.request_cancel(job_id, owner_scope=owner_scope)
        if job is None:
            return None
        with self._tasks_lock:
            task = self._tasks.get(job_id)
            if task is not None:
                task.cancel_event.set()
        return self.job_store.get(job_id, owner_scope=owner_scope)

    def get(self, job_id: str, *, owner_scope: str) -> BacktestJob | None:
        return self.job_store.get(job_id, owner_scope=owner_scope)

    def queue_snapshot(self) -> BacktestQueueSnapshot:
        with self._tasks_lock:
            task_ids = tuple(self._tasks)
        running = 0
        queued = 0
        for job_id in task_ids:
            with self.job_store._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            if row is not None and row["status"] == "running":
                running += 1
            elif row is not None and row["status"] == "queued":
                queued += 1
        return BacktestQueueSnapshot(
            max_concurrent=self.max_concurrent,
            max_queued=self.max_queued,
            running=running,
            queued=queued,
        )

    def close(self, *, cancel_running: bool = True, timeout: float = 5.0) -> None:
        with self._submission_lock:
            if self._closed:
                return
            self._closed = True
        if cancel_running:
            with self._tasks_lock:
                tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel_event.set()
                self.job_store.request_cancel(
                    task.job_id,
                    owner_scope=task.owner_scope,
                )
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        for _worker in self._workers:
            remaining = max(0.0, deadline - datetime.now(timezone.utc).timestamp())
            try:
                self._queue.put(None, timeout=remaining)
            except queue.Full:
                break
        for worker in self._workers:
            remaining = max(0.0, deadline - datetime.now(timezone.utc).timestamp())
            worker.join(timeout=remaining)
        if any(worker.is_alive() for worker in self._workers):
            raise BacktestRuntimeError("backtest workers did not stop before timeout")
        self._lease.__exit__(None, None, None)

    def __enter__(self) -> "BacktestRuntime":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                claimed = self.job_store.claim(task.job_id)
                if claimed is None:
                    continue
                try:
                    prepared = task.execute(task.cancel_event)
                    if (
                        prepared.record.owner_scope != task.owner_scope
                        or prepared.record.provenance.backtest_input_sha256
                        != task.request_sha256
                    ):
                        raise BacktestJobIntegrityError(
                            "prepared BacktestRun does not match persistent job identity"
                        )
                    if task.cancel_event.is_set():
                        self.job_store.cancel_running(
                            task.job_id,
                            diagnostic=BacktestJobDiagnostic(
                                code="JOB_CANCELLED",
                                message="backtest was cancelled before persistence",
                            ),
                        )
                        continue
                    persisted: PersistedBacktestRun = self.run_store.put(
                        prepared,
                        idempotency_key=task.idempotency_key,
                    )
                    self.job_store.complete(
                        task.job_id,
                        run_id=persisted.record.run_id,
                    )
                except WorkerExecutionError as exc:
                    diagnostic = BacktestJobDiagnostic(
                        code=exc.code,
                        message=str(exc)[:2_000] or exc.code,
                        stderr=exc.stderr[-16_384:],
                    )
                    if exc.code == "WORKER_CANCELLED" or task.cancel_event.is_set():
                        self.job_store.cancel_running(
                            task.job_id,
                            diagnostic=diagnostic,
                        )
                    else:
                        self.job_store.fail(task.job_id, diagnostic=diagnostic)
                except Exception as exc:
                    self.job_store.fail(
                        task.job_id,
                        diagnostic=BacktestJobDiagnostic(
                            code="BACKTEST_FAILED",
                            message=str(exc)[:2_000] or type(exc).__name__,
                        ),
                    )
            finally:
                if task is not None:
                    with self._tasks_lock:
                        self._tasks.pop(task.job_id, None)
                self._queue.task_done()

    def _reconcile_startup(self) -> None:
        for job in self.job_store.list_incomplete():
            persisted = self.run_store.get_by_idempotency_key(
                owner_scope=job.owner_scope,
                idempotency_key=job.idempotency_key,
            )
            if persisted is not None and (
                persisted.record.provenance.backtest_input_sha256
                != job.request_sha256
            ):
                raise BacktestJobIntegrityError(
                    "durable run and incomplete job request identities differ"
                )
            self.job_store.reconcile_job(
                job.job_id,
                run_id=persisted.record.run_id if persisted is not None else None,
            )


def backup_backtest_runtime(
    destination: Path,
    *,
    job_store: BacktestJobStore,
    run_store: BacktestRunStore,
) -> BacktestRuntimeBackupResult:
    """Back up quiesced job, research, and run state under one scheduler lease."""

    destination = Path(destination)
    if destination.is_symlink():
        raise BacktestJobIntegrityError("runtime backup must not be a symlink")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise BacktestJobIntegrityError(
            "runtime backup destination must be an empty directory"
        )
    with job_store.scheduler_lease():
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        storage = run_store.backup_to(destination / "storage")
        job_count = job_store.backup_to(destination / "jobs")
        manifest = {
            "schema_version": "vibe.backtest-runtime-backup.v1",
            "job_count": job_count,
            "run_count": storage.run_count,
            "research_object_count": storage.research_object_count,
            "storage_manifest_sha256": storage.manifest_sha256,
        }
        manifest_sha256 = canonical_sha256(manifest)
        payload = (
            canonical_json(
                {
                    **manifest,
                    "manifest_sha256": manifest_sha256,
                }
            )
            + "\n"
        ).encode("utf-8")
        BacktestRunStore._atomic_write(destination / "manifest.json", payload)
    return BacktestRuntimeBackupResult(
        destination=destination,
        job_count=job_count,
        storage=storage,
        manifest_sha256=manifest_sha256,
    )
