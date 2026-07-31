"""Typed main-process boundary for pinned, offline QUANTAXIS operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
import tempfile
from threading import Event
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    DataSnapshotRef,
    EngineRequest,
    ResearchObject,
    canonical_json,
    canonical_sha256,
)
from src.strategy_spec.compiler import StrategyCompilation
from src.strategy_spec.versioning import (
    StrategyConfirmationCard,
    StrategyConfirmationReceipt,
    StrategyHeadToken,
    StrategyVersion,
)

from .cn_equity_accounting import (
    CnEquityAccount,
    CnEquityFeeSchedule,
    CnEquityLedger,
    CnEquityOrder,
)
from .runner import RunResult, WorkerRunner, compute_snapshot_sha256


QUANTAXIS_ENGINE_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"
QUANTAXIS_ENGINE_VERSION = "2.1.0a2"
QUANTAXIS_SOURCE_SHA256 = {
    "data_fq": "8ea6b152a4eff20bffba2216ae8dc88edbb3f0f11b0a220abb19c574cf459eff",
    "indicator_base": "fbbb3debfa0d061eb4d6e56acf783dea18a144f56ffc191769df075cf38c4737",
    "indicators": "94995068dbe73fdeddfea69bd51c0df52af6fc4567c5c432a265c2e3f9363ff5",
    "calendar": "014ff7173c349da78bbf60dcad52227881d9f0a32d958d98973c4914e12acf45",
    "market_preset": "d789ed62fd173ce2102f87c22ec6e7c155f6c07180e383ff49965fbed70bf8fd",
    "position": "531b691e8a8791cf1a5e9136a980f5a6972e2e4f6fd78df994766213f80dda55",
    "qifi_account": "8b1cae0450d7c3cf9fb4f112191724096f09c84fd6d9c11662d2f13020166a9d",
    "parameters": "dfb09865c6d6016cfea5c2a6009b09353fbe6aa5416a2df53ede82d2ca8ab773",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QuantaxisFactorSpec(_StrictModel):
    name: Literal["ma", "ema"]
    window: int = Field(ge=2, le=512)


class QuantaxisBacktestEvent(_StrictModel):
    """One worker decision replayed by the engine-neutral accounting oracle."""

    event: Literal["order", "mark", "share_split", "dividend_ex", "dividend_pay"]
    trade_date: date
    order: CnEquityOrder | None = None
    mark_prices_fen: dict[str, int] | None = None
    symbol: str | None = Field(
        default=None,
        pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$",
    )
    multiplier_numerator: int | None = Field(default=None, gt=0)
    multiplier_denominator: int | None = Field(default=None, gt=0)
    entitled_shares: int | None = Field(default=None, gt=0)
    cash_per_share_fen: int | None = Field(default=None, gt=0)
    market_rule_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )

    @model_validator(mode="after")
    def validate_shape(self) -> "QuantaxisBacktestEvent":
        if self.event == "order":
            if (
                self.order is None
                or self.order.trade_date != self.trade_date
                or self.mark_prices_fen is not None
                or self.symbol is not None
                or self.multiplier_numerator is not None
                or self.multiplier_denominator is not None
                or self.entitled_shares is not None
                or self.cash_per_share_fen is not None
            ):
                raise ValueError("order event requires a same-date order")
            if self.market_rule_id is None:
                raise ValueError("order event requires market_rule_id")
        elif self.order is not None or self.market_rule_id is not None:
            raise ValueError("non-order event cannot contain order data")
        if self.event == "mark":
            if (
                self.mark_prices_fen is None
                or self.symbol is not None
                or self.multiplier_numerator is not None
                or self.multiplier_denominator is not None
                or self.entitled_shares is not None
                or self.cash_per_share_fen is not None
            ):
                raise ValueError("mark event requires mark_prices_fen")
        elif self.event in {"share_split", "dividend_ex", "dividend_pay"}:
            if self.symbol is None or self.mark_prices_fen is None:
                raise ValueError("corporate action requires symbol and marks")
        if self.event == "share_split" and (
            self.multiplier_numerator is None
            or self.multiplier_denominator is None
        ):
            raise ValueError("share_split requires an exact multiplier")
        if self.event == "share_split" and (
            self.entitled_shares is not None or self.cash_per_share_fen is not None
        ):
            raise ValueError("share_split cannot contain dividend data")
        if self.event == "dividend_ex" and (
            self.entitled_shares is None
            or self.cash_per_share_fen is None
            or self.multiplier_numerator is not None
            or self.multiplier_denominator is not None
        ):
            raise ValueError("dividend_ex requires entitlement and cash amount")
        if self.event == "dividend_pay" and any(
            item is not None
            for item in (
                self.multiplier_numerator,
                self.multiplier_denominator,
                self.entitled_shares,
                self.cash_per_share_fen,
            )
        ):
            raise ValueError("dividend_pay cannot contain accrual data")
        return self


class QuantaxisPositionSnapshot(_StrictModel):
    trade_date: date
    positions: dict[str, int]
    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_positions(self) -> "QuantaxisPositionSnapshot":
        if any(quantity <= 0 for quantity in self.positions.values()):
            raise ValueError("engine positions must be positive")
        return self


class QuantaxisRiskPolicy(_StrictModel):
    """Exact confirmed risk thresholds used by the worker."""

    max_drawdown_ppm: int | None = Field(default=None, gt=0, lt=1_000_000)
    max_purchase_turnover_ppm: int | None = Field(default=None, gt=0)


class QuantaxisRiskAudit(_StrictModel):
    trade_date: date
    peak_equity_fen: int = Field(ge=0)
    drawdown_ppm: int = Field(ge=0, le=1_000_000)
    cumulative_purchase_notional_fen: int = Field(ge=0)
    purchase_turnover_ppm: int = Field(ge=0)
    drawdown_halted: bool


class QuantaxisBacktestWorkerResult(_StrictModel):
    operation_schema: Literal[
        "vibe.quantaxis-backtest-result.v1"
    ] = "vibe.quantaxis-backtest-result.v1"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_request_id: str
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backtest_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_table_version: str
    fee_schedule: CnEquityFeeSchedule
    slippage_tenths_bps: int = Field(ge=0)
    risk_policy: QuantaxisRiskPolicy
    opening_date: date
    initial_cash_fen: int = Field(ge=0)
    board_lot: int = Field(gt=0)
    events: tuple[QuantaxisBacktestEvent, ...]
    engine_position_snapshots: tuple[QuantaxisPositionSnapshot, ...]
    signal_audit: tuple[dict[str, Any], ...] = ()
    risk_audit: tuple[QuantaxisRiskAudit, ...]
    engine_version: str
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_timeline(self) -> "QuantaxisBacktestWorkerResult":
        event_dates = tuple(item.trade_date for item in self.events)
        if event_dates != tuple(sorted(event_dates)):
            raise ValueError("backtest events must be chronological")
        snapshot_dates = tuple(item.trade_date for item in self.engine_position_snapshots)
        if snapshot_dates != tuple(sorted(snapshot_dates)) or len(snapshot_dates) != len(
            set(snapshot_dates)
        ):
            raise ValueError("engine position snapshots must be unique and chronological")
        risk_dates = tuple(item.trade_date for item in self.risk_audit)
        if risk_dates != snapshot_dates:
            raise ValueError("risk audit must cover each engine account snapshot")
        return self


@dataclass(frozen=True)
class QuantaxisBacktestResult:
    """Validated worker output plus the independently replayed canonical ledger."""

    worker: QuantaxisBacktestWorkerResult
    ledger: CnEquityLedger


class QuantaxisOperationError(RuntimeError):
    """Stable error returned by a validated but unsuccessful worker call."""

    def __init__(self, code: str, message: str, *, run: RunResult) -> None:
        super().__init__(message)
        self.code = code
        self.run = run


class QuantaxisReconciliationError(ValueError):
    """Worker execution state diverged from the canonical Vibe oracle."""


def _has_symlink_component(path: Path) -> bool:
    current = path.absolute()
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def write_quantaxis_backtest_snapshot(
    snapshot: Mapping[str, Any],
    path: Path,
) -> str:
    """Persist canonical QE5 worker input atomically without replacing history."""

    target = Path(path).absolute()
    if _has_symlink_component(target):
        raise ValueError("QUANTAXIS backtest snapshot path must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(target):
        raise ValueError("QUANTAXIS backtest snapshot path must not be a symlink")
    payload = canonical_json(snapshot).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.stem}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, target)
        except FileExistsError:
            if (
                _has_symlink_component(target)
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != digest
            ):
                raise ValueError(
                    "existing QUANTAXIS backtest snapshot has different content"
                )
        directory_descriptor = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return digest


class QuantaxisAdapter:
    """Facade over one exact :class:`WorkerRunner` instance."""

    def __init__(self, runner: WorkerRunner) -> None:
        if runner.config.engine.name != "quantaxis":
            raise ValueError("QuantaxisAdapter requires the quantaxis engine")
        if runner.config.engine.commit != QUANTAXIS_ENGINE_COMMIT:
            raise ValueError("QuantaxisAdapter requires the audited QUANTAXIS commit")
        self.runner = runner

    def _run(
        self,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, Any],
        snapshot_path: Path,
        timeout_seconds: float,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        memory_bytes: int | None = None,
        max_open_files: int | None = None,
        cancel_event: Event | None = None,
    ) -> Mapping[str, Any]:
        path = snapshot_path.absolute()
        snapshot_sha256 = compute_snapshot_sha256(path)
        run_kwargs: dict[str, Any] = {
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
            "snapshot_path": str(path),
            "snapshot_sha256": snapshot_sha256,
            "timeout_seconds": timeout_seconds,
        }
        if max_stdout_bytes is not None:
            run_kwargs["max_stdout_bytes"] = max_stdout_bytes
        if max_stderr_bytes is not None:
            run_kwargs["max_stderr_bytes"] = max_stderr_bytes
        if memory_bytes is not None:
            run_kwargs["memory_bytes"] = memory_bytes
        if max_open_files is not None:
            run_kwargs["max_open_files"] = max_open_files
        if cancel_event is not None:
            run_kwargs["cancel_event"] = cancel_event
        run = self.runner.run(
            **run_kwargs,
        )
        response = run.response
        if response["status"] != "ok":
            error = response["error"]
            raise QuantaxisOperationError(error["code"], error["message"], run=run)
        result = response["result"]
        if result.get("snapshot_sha256") != snapshot_sha256:
            raise QuantaxisOperationError(
                "SNAPSHOT_RESULT_MISMATCH",
                "worker result did not bind the input snapshot hash",
                run=run,
            )
        return result

    def adjust_prices(
        self,
        *,
        request_id: str,
        symbol: str,
        adjustment: Literal["qfq", "hfq"],
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        return self._run(
            request_id=request_id,
            operation="adjust_prices",
            payload={"symbol": symbol, "adjustment": adjustment},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )

    def trading_calendar(
        self,
        *,
        request_id: str,
        start_date: date,
        end_date: date,
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if start_date > end_date:
            raise ValueError("start_date must not follow end_date")
        return self._run(
            request_id=request_id,
            operation="trading_calendar",
            payload={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )

    def compute_factors(
        self,
        *,
        request_id: str,
        symbol: str,
        factors: tuple[QuantaxisFactorSpec, ...],
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if not factors or len(factors) > 16:
            raise ValueError("factors must contain 1 to 16 specifications")
        identities = [(item.name, item.window) for item in factors]
        if len(set(identities)) != len(identities):
            raise ValueError("factor specifications must be unique")
        return self._run(
            request_id=request_id,
            operation="compute_factors",
            payload={"symbol": symbol, "factors": [item.model_dump() for item in factors]},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _require_confirmed(
        *,
        compilation: StrategyCompilation,
        version: StrategyVersion,
        head: StrategyHeadToken,
        card: StrategyConfirmationCard,
        receipt: StrategyConfirmationReceipt,
        snapshot: ResearchObject,
    ) -> tuple[EngineRequest, DataSnapshotRef]:
        if head.state != "confirmed":
            raise ValueError("strategy head must be confirmed before backtest")
        if (
            head.stream_id != version.stream_id
            or head.version_id != version.version_id
            or card.stream_id != version.stream_id
            or card.version_id != version.version_id
            or receipt.stream_id != version.stream_id
            or receipt.version_id != version.version_id
        ):
            raise ValueError("confirmation lineage does not match strategy version")
        if receipt.confirmation_hash != card.confirmation_hash:
            raise ValueError("confirmation receipt does not match exact card")
        if version.strategy is None or version.strategy_spec_ref is None:
            raise ValueError("confirmed strategy version has no executable strategy")
        if (
            card.strategy != version.strategy
            or card.strategy_spec_ref != version.strategy_spec_ref
            or compilation.plan.strategy != version.strategy
            or compilation.plan.strategy_spec_ref != version.strategy_spec_ref
        ):
            raise ValueError("compiled plan does not match confirmed strategy")
        request = compilation.engine_request.payload
        if not isinstance(request, EngineRequest):
            raise TypeError("compilation does not contain EngineRequest")
        if snapshot.ref() != compilation.plan.data_snapshot_ref:
            raise ValueError("compiled plan does not reference the supplied snapshot")
        if not isinstance(snapshot.payload, DataSnapshotRef):
            raise TypeError("snapshot must contain DataSnapshotRef")
        if version.strategy.data_snapshot_ref != snapshot.ref():
            raise ValueError("confirmed strategy does not reference the supplied snapshot")
        return request, snapshot.payload

    @staticmethod
    def _execution_bindings(
        compilation: StrategyCompilation,
    ) -> tuple[CnEquityFeeSchedule, int, QuantaxisRiskPolicy]:
        costs = compilation.plan.strategy.costs

        def exact_scaled(value: float, scale: int, label: str) -> int:
            converted = round(value * scale)
            if abs(converted / scale - value) > 1e-9:
                raise ValueError(
                    f"{label} must be expressible at scale {scale}"
                )
            return converted

        fees = CnEquityFeeSchedule(
            commission_tenths_bps=exact_scaled(
                costs.commission_bps,
                10,
                "commission_bps",
            ),
            minimum_commission_fen=exact_scaled(
                costs.minimum_commission,
                100,
                "minimum_commission",
            ),
            sell_tax_tenths_bps=exact_scaled(
                costs.sell_tax_bps,
                10,
                "sell_tax_bps",
            ),
            transfer_fee_tenths_bps=exact_scaled(
                costs.transfer_fee_bps,
                10,
                "transfer_fee_bps",
            ),
            rule_version=costs.rule_version,
        )
        risk = compilation.plan.strategy.risk
        policy = QuantaxisRiskPolicy(
            max_drawdown_ppm=(
                None
                if risk.max_drawdown_stop is None
                else exact_scaled(
                    risk.max_drawdown_stop,
                    1_000_000,
                    "max_drawdown_stop",
                )
            ),
            max_purchase_turnover_ppm=(
                None
                if risk.max_turnover is None
                else exact_scaled(
                    risk.max_turnover,
                    1_000_000,
                    "max_turnover",
                )
            ),
        )
        return (
            fees,
            exact_scaled(costs.slippage_bps, 10, "slippage_bps"),
            policy,
        )

    @staticmethod
    def _replay_oracle(
        result: QuantaxisBacktestWorkerResult,
    ) -> CnEquityLedger:
        account = CnEquityAccount(
            ledger_id=f"quantaxis:{result.engine_request_id}",
            data_snapshot_sha256=result.snapshot_sha256,
            opening_date=result.opening_date,
            initial_cash_fen=result.initial_cash_fen,
            rules=result.fee_schedule,
            board_lot=result.board_lot,
        )
        snapshots = {
            item.trade_date: item
            for item in result.engine_position_snapshots
        }
        reconciled_dates: set[date] = set()
        for event in result.events:
            if event.event == "order":
                assert event.order is not None
                account.submit_order(event.order)
            elif event.event == "mark":
                assert event.mark_prices_fen is not None
                account.mark(
                    trade_date=event.trade_date,
                    mark_prices_fen=event.mark_prices_fen,
                )
                expected = snapshots.get(event.trade_date)
                actual = account.ledger().entries[-1]
                if expected is None or (
                    account.positions != expected.positions
                    or account.cash_fen != expected.cash_fen
                    or actual.dividend_receivable_fen
                    != expected.dividend_receivable_fen
                    or actual.equity_fen != expected.equity_fen
                ):
                    raise QuantaxisReconciliationError(
                        f"QUANTAXIS account state diverged on {event.trade_date.isoformat()}",
                    )
                reconciled_dates.add(event.trade_date)
            elif event.event == "share_split":
                assert event.symbol is not None
                assert event.multiplier_numerator is not None
                assert event.multiplier_denominator is not None
                assert event.mark_prices_fen is not None
                account.apply_share_split(
                    trade_date=event.trade_date,
                    symbol=event.symbol,
                    multiplier_numerator=event.multiplier_numerator,
                    multiplier_denominator=event.multiplier_denominator,
                    mark_prices_fen=event.mark_prices_fen,
                )
            elif event.event == "dividend_ex":
                assert event.symbol is not None
                assert event.entitled_shares is not None
                assert event.cash_per_share_fen is not None
                assert event.mark_prices_fen is not None
                account.accrue_dividend(
                    trade_date=event.trade_date,
                    symbol=event.symbol,
                    entitled_shares=event.entitled_shares,
                    cash_per_share_fen=event.cash_per_share_fen,
                    mark_prices_fen=event.mark_prices_fen,
                )
            else:
                assert event.symbol is not None
                assert event.mark_prices_fen is not None
                account.pay_dividend(
                    trade_date=event.trade_date,
                    symbol=event.symbol,
                    mark_prices_fen=event.mark_prices_fen,
                )
        if reconciled_dates != set(snapshots):
            raise QuantaxisReconciliationError(
                "worker account snapshots do not match daily mark events"
            )
        return account.ledger()

    def backtest(
        self,
        *,
        compilation: StrategyCompilation,
        version: StrategyVersion,
        head: StrategyHeadToken,
        card: StrategyConfirmationCard,
        receipt: StrategyConfirmationReceipt,
        snapshot: ResearchObject,
        snapshot_path: Path,
        initial_cash_fen: int,
        cancel_event: Event | None = None,
    ) -> QuantaxisBacktestResult:
        """Run one exact confirmed plan and reconcile every daily position."""

        if (
            isinstance(initial_cash_fen, bool)
            or not isinstance(initial_cash_fen, int)
            or initial_cash_fen < 0
        ):
            raise ValueError("initial_cash_fen must be a non-negative integer")
        request, snapshot_ref = self._require_confirmed(
            compilation=compilation,
            version=version,
            head=head,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
        )
        if request.engine.name != "quantaxis" or request.engine.commit != QUANTAXIS_ENGINE_COMMIT:
            raise ValueError("EngineRequest does not target the audited QUANTAXIS engine")
        actual_snapshot_sha256 = compute_snapshot_sha256(snapshot_path.absolute())
        if snapshot_ref.snapshot_sha256 != actual_snapshot_sha256:
            raise ValueError("DataSnapshotRef does not bind the supplied snapshot bytes")
        expected_fees, expected_slippage, expected_risk = self._execution_bindings(
            compilation
        )
        worker_payload = {
            "schema_version": "vibe.quantaxis-backtest-request.v1",
            "engine_request": request.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot_ref.model_dump(mode="json"),
            "confirmation": {
                "version_id": version.version_id,
                "card_id": card.card_id,
                "receipt_id": receipt.receipt_id,
                "confirmation_hash": receipt.confirmation_hash,
            },
            "initial_cash_fen": initial_cash_fen,
        }
        expected_input_sha256 = canonical_sha256(worker_payload)
        raw = self._run(
            request_id=request.request_id,
            operation="backtest",
            payload=worker_payload,
            snapshot_path=snapshot_path,
            timeout_seconds=request.resource_limits.timeout_seconds,
            max_stdout_bytes=request.resource_limits.max_stdout_bytes,
            max_stderr_bytes=request.resource_limits.max_stderr_bytes,
            memory_bytes=request.resource_limits.memory_bytes,
            max_open_files=256,
            cancel_event=cancel_event,
        )
        result = QuantaxisBacktestWorkerResult.model_validate(raw)
        if (
            result.snapshot_sha256 != actual_snapshot_sha256
            or result.engine_request_id != request.request_id
            or result.execution_plan_sha256 != compilation.plan.content_sha256
            or result.backtest_input_sha256 != expected_input_sha256
        ):
            raise ValueError("worker backtest result identity does not match request")
        if result.fee_schedule != expected_fees:
            raise ValueError("worker fee schedule does not match confirmed strategy")
        if (
            result.slippage_tenths_bps != expected_slippage
            or result.risk_policy != expected_risk
        ):
            raise ValueError("worker execution bindings do not match confirmed strategy")
        if result.board_lot != compilation.plan.strategy.execution.board_lot:
            raise ValueError("worker board lot does not match confirmed strategy")
        if (
            result.engine_version != QUANTAXIS_ENGINE_VERSION
            or result.source_sha256 != QUANTAXIS_SOURCE_SHA256
        ):
            raise ValueError("worker engine provenance does not match audited QUANTAXIS")
        ledger = self._replay_oracle(result)
        return QuantaxisBacktestResult(worker=result, ledger=ledger)
