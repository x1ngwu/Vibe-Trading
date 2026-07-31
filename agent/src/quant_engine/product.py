"""QE5-5 authenticated product projection over the persistent backtest runtime."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    DEFAULT_OWNER_SCOPE,
    EngineIdentitySpec,
    ResourceLimits,
    canonical_json,
    canonical_sha256,
)
from src.research.store import ResearchStore
from src.strategy_spec.compiler import StrategyCompilation, compile_strategy_template
from src.strategy_spec.presentation import default_strategy_version_db_path
from src.strategy_spec.templates import StrategyTemplateBuild
from src.strategy_spec.version_store import StrategyVersionStore

from .backtest_run import (
    BacktestRunStore,
    NormalizedBacktestRecord,
    normalize_quantaxis_backtest_run,
)
from .backtest_runtime import (
    BacktestJob,
    BacktestJobStore,
    BacktestRuntime,
    BacktestSubmitResult,
)
from .protocol import EngineIdentity
from .quantaxis_adapter import QUANTAXIS_ENGINE_COMMIT, QuantaxisAdapter
from .runner import WorkerConfig, WorkerRunner, compute_snapshot_sha256


_SAFE_ID = r"^[A-Za-z0-9_-]{1,128}$"
_STREAM_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_VERSION_ID = r"^strategy-version:[0-9a-f]{64}$"
_JOB_ID = r"^backtest-job:[0-9a-f]{64}$"
_RUN_ID = r"^backtest-record:[0-9a-f]{64}$"
_IDEMPOTENCY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_TEMPLATE_IDS = (
    "top_n_rebalance",
    "factor_threshold",
    "factor_trend_confirmation",
)


class BacktestProductError(ValueError):
    """Stable fail-closed error at the authenticated product boundary."""


class _ProductModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestResultVisualizationSpec(_ProductModel):
    """Small content-bound reference safe to persist in chat history."""

    schema_version: Literal[1] = 1
    type: Literal["backtest_result"] = "backtest_result"
    visualization_id: str = Field(pattern=_SAFE_ID)
    data_ref: str = Field(pattern=_SAFE_ID)
    title: str = Field(min_length=1, max_length=200)
    stream_id: str = Field(pattern=_STREAM_ID)
    strategy_version_id: str = Field(pattern=_VERSION_ID)
    strategy_version_number: int = Field(ge=1)
    job_id: str = Field(pattern=_JOB_ID)
    fallback_text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_identity(self) -> "BacktestResultVisualizationSpec":
        if self.data_ref != self.visualization_id:
            raise ValueError("data_ref must equal visualization_id")
        return self


class BacktestEquityPoint(_ProductModel):
    time: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    equity: int = Field(ge=0)
    drawdown: float = Field(ge=-1.0, le=0.0)


class BacktestTradeMarker(_ProductModel):
    time: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    symbol: str
    side: Literal["BUY", "SELL"]
    price: float = Field(gt=0)
    qty: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class BacktestDiagnosticView(_ProductModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    trade_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    kind: str | None = Field(default=None, max_length=64)


class BacktestResultPayload(_ProductModel):
    """Bounded UI projection; the immutable normalized record remains truth."""

    schema_version: Literal["vibe.backtest-product.v1"] = "vibe.backtest-product.v1"
    visualization_id: str = Field(pattern=_SAFE_ID)
    type: Literal["backtest_result"] = "backtest_result"
    stream_id: str = Field(pattern=_STREAM_ID)
    strategy_version_id: str = Field(pattern=_VERSION_ID)
    strategy_version_number: int = Field(ge=1)
    job_id: str = Field(pattern=_JOB_ID)
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_id: str | None = Field(default=None, pattern=_RUN_ID)
    metrics: dict[str, float | int | None] | None = None
    equity: tuple[BacktestEquityPoint, ...] = ()
    trades: tuple[BacktestTradeMarker, ...] = ()
    diagnostics: tuple[BacktestDiagnosticView, ...] = ()
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ledger_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    engine_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    truncated_equity: bool = False
    truncated_trades: bool = False
    truncated_diagnostics: bool = False


class BacktestMetricDelta(_ProductModel):
    metric: Literal[
        "total_return",
        "annualized_return",
        "max_drawdown",
        "turnover",
        "trade_count",
    ]
    left: float | int | None
    right: float | int | None
    delta: float | int | None


class BacktestComparisonPayload(_ProductModel):
    schema_version: Literal["vibe.backtest-comparison.v1"] = (
        "vibe.backtest-comparison.v1"
    )
    stream_id: str = Field(pattern=_STREAM_ID)
    left_job_id: str = Field(pattern=_JOB_ID)
    right_job_id: str = Field(pattern=_JOB_ID)
    left_version_id: str = Field(pattern=_VERSION_ID)
    right_version_id: str = Field(pattern=_VERSION_ID)
    deltas: tuple[BacktestMetricDelta, ...]


def _sample(values: tuple[Any, ...], limit: int) -> tuple[Any, ...]:
    if len(values) <= limit:
        return values
    if limit <= 1:
        return values[:limit]
    indexes = {
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    }
    return tuple(values[index] for index in sorted(indexes))


def _infer_build(strategy_object) -> StrategyTemplateBuild:
    strategy = strategy_object.payload
    source_mode = (
        "similarity_run"
        if getattr(strategy, "similarity_run_ref", None) is not None
        else "direct"
    )
    for template_id in _TEMPLATE_IDS:
        try:
            return StrategyTemplateBuild(
                template_id=template_id,
                source_mode=source_mode,
                strategy_object=strategy_object,
            )
        except ValueError:
            continue
    raise BacktestProductError("confirmed strategy does not match an executable template")


def _request_identity(
    *,
    compilation: StrategyCompilation,
    version,
    card,
    receipt,
    snapshot,
    initial_cash_fen: int,
) -> str:
    return canonical_sha256(
        {
            "schema_version": "vibe.quantaxis-backtest-request.v1",
            "engine_request": compilation.engine_request.payload.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
            "confirmation": {
                "version_id": version.version_id,
                "card_id": card.card_id,
                "receipt_id": receipt.receipt_id,
                "confirmation_hash": receipt.confirmation_hash,
            },
            "initial_cash_fen": initial_cash_fen,
        }
    )


def _visualization_spec(job: BacktestJob, *, version) -> BacktestResultVisualizationSpec:
    digest = job.job_id.split(":", 1)[1]
    visualization_id = f"backtest_{digest[:24]}"
    return BacktestResultVisualizationSpec(
        visualization_id=visualization_id,
        data_ref=visualization_id,
        title=f"{version.strategy.title} · 回测结果",
        stream_id=version.stream_id,
        strategy_version_id=version.version_id,
        strategy_version_number=version.version_number,
        job_id=job.job_id,
        fallback_text="回测状态暂时不可用，请刷新重试。",
    )


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def persist_backtest_visualization(
    run_dir: Path,
    spec: BacktestResultVisualizationSpec,
) -> None:
    """Merge one job reference into the bounded chat visualization manifest."""

    manifest_path = run_dir / "artifacts" / "visualizations.json"
    manifest: list[dict[str, Any]] = []
    try:
        if manifest_path.is_file() and manifest_path.stat().st_size <= 256_000:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                manifest = [item for item in raw if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError):
        manifest = []
    serialized = spec.model_dump(mode="json")
    manifest = [
        item
        for item in manifest
        if item.get("visualization_id") != spec.visualization_id
    ]
    manifest.append(serialized)
    _atomic_write_json(manifest_path, manifest[-5:])


class BacktestProductService:
    """Resolve confirmed strategies and expose one persistent runtime to products."""

    def __init__(
        self,
        *,
        research_store: ResearchStore,
        version_db_path: Path,
        snapshot_root: Path,
        run_store: BacktestRunStore,
        job_store: BacktestJobStore,
        runtime: BacktestRuntime,
        adapter: QuantaxisAdapter,
        resource_limits: ResourceLimits | None = None,
        random_seed: int = 0,
    ) -> None:
        self.research_store = research_store
        self.version_db_path = Path(version_db_path)
        self.snapshot_root = Path(snapshot_root)
        self.run_store = run_store
        self.job_store = job_store
        self.runtime = runtime
        self.adapter = adapter
        self.resource_limits = resource_limits or ResourceLimits(
            timeout_seconds=60,
            max_stdout_bytes=8_388_608,
            max_stderr_bytes=2_097_152,
            # RLIMIT_AS bounds virtual address space, not resident memory.
            # numpy/pandas shared objects need mapping headroom while PR-03
            # independently gates measured peak RSS at 1 GiB.
            memory_bytes=2_147_483_648,
        )
        self.random_seed = random_seed

    def submit(
        self,
        *,
        session_id: str,
        strategy_version_id: str,
        idempotency_key: str,
        initial_cash_fen: int,
    ) -> tuple[BacktestSubmitResult, BacktestResultVisualizationSpec]:
        if not re.fullmatch(_STREAM_ID, session_id):
            raise BacktestProductError("session_id is invalid")
        if not re.fullmatch(_VERSION_ID, strategy_version_id):
            raise BacktestProductError("strategy_version_id is invalid")
        if not re.fullmatch(_IDEMPOTENCY, idempotency_key):
            raise BacktestProductError("idempotency_key is invalid")
        if (
            isinstance(initial_cash_fen, bool)
            or not isinstance(initial_cash_fen, int)
            or initial_cash_fen <= 0
        ):
            raise BacktestProductError("initial_cash_fen must be a positive integer")

        with StrategyVersionStore(self.version_db_path) as versions:
            version = versions.get_version(strategy_version_id)
            head = versions.get_head(session_id)
            if (
                version is None
                or version.stream_id != session_id
                or version.owner_scope != DEFAULT_OWNER_SCOPE
            ):
                raise BacktestProductError("strategy version was not found in this session")
            if (
                head is None
                or head.state != "confirmed"
                or head.version_id != version.version_id
            ):
                raise BacktestProductError("only the exact confirmed strategy head can run")
            events = versions.list_events(session_id)
            confirmation_hash = events[-1].confirmation_hash if events else None
            if confirmation_hash is None:
                raise BacktestProductError("confirmed strategy has no confirmation hash")
            card = versions.get_confirmation_card(confirmation_hash)
            receipt = versions.get_confirmation_receipt_for_card(
                stream_id=session_id,
                confirmation_hash=confirmation_hash,
            )
            if (
                card is None
                or receipt is None
                or card.version_id != version.version_id
                or receipt.version_id != version.version_id
            ):
                raise BacktestProductError("confirmed strategy chain is incomplete")

        if version.strategy is None or version.strategy_spec_ref is None:
            raise BacktestProductError("confirmed strategy is not executable")
        strategy_object = self.research_store.get(
            version.strategy_spec_ref.object_id,
            owner_scope=DEFAULT_OWNER_SCOPE,
        )
        snapshot_object = self.research_store.get(
            version.strategy.data_snapshot_ref.object_id,
            owner_scope=DEFAULT_OWNER_SCOPE,
        )
        if (
            strategy_object is None
            or strategy_object.ref() != version.strategy_spec_ref
            or snapshot_object is None
            or snapshot_object.ref() != version.strategy.data_snapshot_ref
        ):
            raise BacktestProductError("strategy or snapshot object is missing")
        build = _infer_build(strategy_object)
        compilation = compile_strategy_template(
            build,
            snapshot=snapshot_object,
            engine=EngineIdentitySpec(
                name="quantaxis",
                commit=QUANTAXIS_ENGINE_COMMIT,
            ),
            resource_limits=self.resource_limits,
            random_seed=self.random_seed,
        )
        snapshot_sha256 = snapshot_object.payload.snapshot_sha256
        snapshot_path = self.snapshot_root / f"{snapshot_sha256}.json"
        try:
            resolved = snapshot_path.resolve(strict=True)
            root = self.snapshot_root.resolve(strict=True)
        except OSError as exc:
            raise BacktestProductError(
                "confirmed snapshot is not materialized for backtest"
            ) from exc
        if (
            root not in resolved.parents
            or snapshot_path.is_symlink()
            or not snapshot_path.is_file()
            or compute_snapshot_sha256(snapshot_path) != snapshot_sha256
        ):
            raise BacktestProductError("materialized snapshot failed identity validation")
        self.research_store.put(compilation.engine_request)
        request_sha256 = _request_identity(
            compilation=compilation,
            version=version,
            card=card,
            receipt=receipt,
            snapshot=snapshot_object,
            initial_cash_fen=initial_cash_fen,
        )

        def execute(cancel_event: threading.Event):
            result = self.adapter.backtest(
                compilation=compilation,
                version=version,
                head=head,
                card=card,
                receipt=receipt,
                snapshot=snapshot_object,
                snapshot_path=snapshot_path,
                initial_cash_fen=initial_cash_fen,
                cancel_event=cancel_event,
            )
            return normalize_quantaxis_backtest_run(
                result,
                compilation=compilation,
                version=version,
                card=card,
                receipt=receipt,
                snapshot=snapshot_object,
            )

        submitted = self.runtime.submit(
            owner_scope=DEFAULT_OWNER_SCOPE,
            idempotency_key=(
                "product:"
                + canonical_sha256(
                    {
                        "session_id": session_id,
                        "idempotency_key": idempotency_key,
                    }
                )
            ),
            request_sha256=request_sha256,
            execute=execute,
            strategy_stream_id=session_id,
            strategy_version_id=version.version_id,
        )
        return submitted, _visualization_spec(submitted.job, version=version)

    def get_job(self, *, session_id: str, job_id: str) -> BacktestJob | None:
        job = self.runtime.get(job_id, owner_scope=DEFAULT_OWNER_SCOPE)
        if job is None or job.strategy_stream_id != session_id:
            return None
        return job

    def cancel(self, *, session_id: str, job_id: str) -> BacktestJob | None:
        current = self.get_job(session_id=session_id, job_id=job_id)
        if current is None:
            return None
        return self.runtime.cancel(job_id, owner_scope=DEFAULT_OWNER_SCOPE)

    def list_jobs(self, *, session_id: str, limit: int = 100) -> tuple[BacktestJob, ...]:
        return tuple(
            job
            for job in self.job_store.list(
                owner_scope=DEFAULT_OWNER_SCOPE,
                limit=limit,
            )
            if job.strategy_stream_id == session_id
        )

    def payload(
        self,
        *,
        session_id: str,
        job_id: str,
    ) -> BacktestResultPayload | None:
        job = self.get_job(session_id=session_id, job_id=job_id)
        if job is None or job.strategy_version_id is None:
            return None
        with StrategyVersionStore(self.version_db_path) as versions:
            version = versions.get_version(job.strategy_version_id)
        if version is None or version.stream_id != session_id:
            raise BacktestProductError("job references a missing strategy version")
        spec = _visualization_spec(job, version=version)
        record = None
        if job.status == "completed":
            if job.run_id is None:
                raise BacktestProductError("completed job has no run")
            persisted = self.run_store.get(
                job.run_id,
                owner_scope=DEFAULT_OWNER_SCOPE,
            )
            if persisted is None:
                raise BacktestProductError("completed job run is missing")
            record = persisted.record
            if record.provenance.strategy_version_id != job.strategy_version_id:
                raise BacktestProductError("job and run strategy versions differ")
        return self._payload_from(job=job, version=version, spec=spec, record=record)

    def compare(
        self,
        *,
        session_id: str,
        left_job_id: str,
        right_job_id: str,
    ) -> BacktestComparisonPayload:
        left = self.payload(session_id=session_id, job_id=left_job_id)
        right = self.payload(session_id=session_id, job_id=right_job_id)
        if left is None or right is None:
            raise BacktestProductError("comparison job was not found in this session")
        if left.metrics is None or right.metrics is None:
            raise BacktestProductError("only completed backtests can be compared")
        names = (
            "total_return",
            "annualized_return",
            "max_drawdown",
            "turnover",
            "trade_count",
        )
        deltas: list[BacktestMetricDelta] = []
        for name in names:
            left_value = left.metrics[name]
            right_value = right.metrics[name]
            delta = (
                None
                if left_value is None or right_value is None
                else right_value - left_value
            )
            deltas.append(
                BacktestMetricDelta(
                    metric=name,
                    left=left_value,
                    right=right_value,
                    delta=delta,
                )
            )
        return BacktestComparisonPayload(
            stream_id=session_id,
            left_job_id=left.job_id,
            right_job_id=right.job_id,
            left_version_id=left.strategy_version_id,
            right_version_id=right.strategy_version_id,
            deltas=tuple(deltas),
        )

    @staticmethod
    def _payload_from(
        *,
        job: BacktestJob,
        version,
        spec: BacktestResultVisualizationSpec,
        record: NormalizedBacktestRecord | None,
    ) -> BacktestResultPayload:
        diagnostics: list[BacktestDiagnosticView] = []
        if job.diagnostic is not None:
            diagnostics.append(
                BacktestDiagnosticView(
                    code=job.diagnostic.code,
                    message=job.diagnostic.message,
                )
            )
        if record is None:
            return BacktestResultPayload(
                visualization_id=spec.visualization_id,
                stream_id=version.stream_id,
                strategy_version_id=version.version_id,
                strategy_version_number=version.version_number,
                job_id=job.job_id,
                status=job.status,
                submitted_at=job.submitted_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                diagnostics=tuple(diagnostics),
            )

        peak = record.provenance.initial_cash_fen
        equity_points: list[BacktestEquityPoint] = []
        for point in record.daily_equity:
            peak = max(peak, point.equity_fen)
            drawdown = point.equity_fen / peak - 1.0 if peak else 0.0
            equity_points.append(
                BacktestEquityPoint(
                    time=point.trade_date.isoformat(),
                    equity=point.equity_fen,
                    drawdown=drawdown,
                )
            )
        trades = tuple(
            BacktestTradeMarker(
                time=fill.trade_date.isoformat(),
                symbol=fill.symbol,
                side=fill.side.upper(),
                price=fill.price_fen / 100.0,
                qty=fill.filled_shares,
                reason=fill.reason,
            )
            for fill in record.fills
        )
        diagnostics.extend(
            BacktestDiagnosticView(
                code=item.code,
                message=item.payload_json[:2_000],
                trade_date=(
                    item.trade_date.isoformat()
                    if item.trade_date is not None
                    else None
                ),
                kind=item.kind,
            )
            for item in record.diagnostics
        )
        sampled_equity = _sample(tuple(equity_points), 1_000)
        sampled_trades = _sample(trades, 1_000)
        sampled_diagnostics = tuple(diagnostics[:200])
        return BacktestResultPayload(
            visualization_id=spec.visualization_id,
            stream_id=version.stream_id,
            strategy_version_id=version.version_id,
            strategy_version_number=version.version_number,
            job_id=job.job_id,
            status=job.status,
            submitted_at=job.submitted_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            run_id=record.run_id,
            metrics=record.metrics.model_dump(mode="json"),
            equity=sampled_equity,
            trades=sampled_trades,
            diagnostics=sampled_diagnostics,
            snapshot_sha256=record.provenance.snapshot_sha256,
            ledger_sha256=record.ledger_sha256,
            engine_commit=record.provenance.engine.commit,
            truncated_equity=len(sampled_equity) < len(equity_points),
            truncated_trades=len(sampled_trades) < len(trades),
            truncated_diagnostics=len(sampled_diagnostics) < len(diagnostics),
        )


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_SERVICE: BacktestProductService | None = None


def default_backtest_product_service() -> BacktestProductService:
    """Open the backed-up household stores and one process-wide scheduler."""

    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is not None:
            return _DEFAULT_SERVICE
        root = Path.home() / ".vibe-trading"
        snapshot_root = root / "backtest-snapshots"
        snapshot_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        research_store = ResearchStore.default()
        run_store = BacktestRunStore(
            root / "backtests" / "runs",
            research_store=research_store,
        )
        job_store = BacktestJobStore(root / "backtests" / "jobs")
        python_text = os.environ.get("VIBE_QE0_QUANTAXIS_PYTHON", "")
        if not python_text:
            raise BacktestProductError(
                "QUANTAXIS worker is not configured; set VIBE_QE0_QUANTAXIS_PYTHON"
            )
        agent_root = Path(__file__).resolve().parents[2]
        try:
            runner = WorkerRunner(
                WorkerConfig(
                    engine=EngineIdentity("quantaxis", QUANTAXIS_ENGINE_COMMIT),
                    python=Path(python_text).absolute(),
                    script=(
                        agent_root / "engine_workers" / "quantaxis" / "worker.py"
                    ).resolve(),
                    snapshot_root=snapshot_root.resolve(),
                ),
                common_runtime=(agent_root / "engine_workers" / "common").resolve(),
            )
        except (OSError, ValueError) as exc:
            raise BacktestProductError(
                "QUANTAXIS worker configuration failed validation"
            ) from exc
        runtime = BacktestRuntime(job_store=job_store, run_store=run_store)
        _DEFAULT_SERVICE = BacktestProductService(
            research_store=research_store,
            version_db_path=default_strategy_version_db_path(),
            snapshot_root=snapshot_root,
            run_store=run_store,
            job_store=job_store,
            runtime=runtime,
            adapter=QuantaxisAdapter(runner),
        )
        return _DEFAULT_SERVICE


def close_default_backtest_product_service() -> None:
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        service = _DEFAULT_SERVICE
        _DEFAULT_SERVICE = None
    if service is not None:
        service.runtime.close()
