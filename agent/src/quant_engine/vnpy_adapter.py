"""QE6-1 boundary for replaying the exact QE5 request through vn.py."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    DataSnapshotRef,
    EngineRequest,
    ResearchObject,
    canonical_sha256,
)
from src.strategy_spec.compiler import StrategyCompilation

from .cn_equity_accounting import CnEquityLedgerEntry
from .oracle_governance import HistoricalRunIdentity
from .quantaxis_adapter import (
    QUANTAXIS_ENGINE_COMMIT,
    QUANTAXIS_ENGINE_VERSION,
    QUANTAXIS_SOURCE_SHA256,
    QuantaxisBacktestResult,
)
from .reconciliation import (
    ReconciliationAccountState,
    ReconciliationArtifact,
    ReconciliationCheckpoint,
    ReconciliationEngineIdentity,
    ReconciliationObservation,
    ReconciliationStore,
    StrategyValidationDecision,
    create_reconciliation_artifact,
    decide_strategy_validation,
)
from .runner import RunResult, WorkerRunner, compute_snapshot_sha256


VNPY_ENGINE_COMMIT = "1b78494979deb4c4996f6b864f234d9839f2f239"
VNPY_ENGINE_VERSION = "4.4.0"
VNPY_SOURCE_SHA256 = {
    "package_init": "ba16287a3acd984a6e68c3e373441c7e8af623ad9df74837227368a4456915a8",
    "event_init": "81752eb9db5a9e9bdf7024f9a8821cf569c5994eceff9194a6ecd725dc8ed365",
    "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
    "trader_init": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "trader_locale_init": "3138d59a9d7bf99cdcc683778e402f0dd7fabcf9698c3ec2820e44508ac0661c",
    "trader_object": "bd360fc224ce22a3f7521bef67ea61125d43c410fa880b9667030ee254546a69",
    "trader_constant": "1361eb485eda9fd97bee3e68324d9b08a96fe927c37a99f8b74de981141e7a0f",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VnpyIdentityEvent(_StrictModel):
    kind: Literal["engine_request", "execution_plan", "data_snapshot"]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VnpyEventPathResult(_StrictModel):
    operation_schema: Literal[
        "vibe.vnpy-event-path-result.v1"
    ] = "vibe.vnpy-event-path-result.v1"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_request_id: str
    engine_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_path_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: tuple[VnpyIdentityEvent, ...]
    engine_version: str
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_events(self) -> "VnpyEventPathResult":
        if tuple(event.kind for event in self.events) != (
            "engine_request",
            "execution_plan",
            "data_snapshot",
        ):
            raise ValueError("vn.py identity events are incomplete or unordered")
        return self


class VnpyOrdinaryOrder(_StrictModel):
    source_sequence: int = Field(ge=0)
    order_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    requested_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    status: Literal["filled", "rejected"]
    reason: Literal[
        "INSUFFICIENT_CASH",
        "NO_POSITION",
        "INSUFFICIENT_POSITION",
    ] | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "VnpyOrdinaryOrder":
        if (self.status == "filled") != (self.reason is None):
            raise ValueError("ordinary order status and reason disagree")
        return self


class VnpyOrdinaryFill(_StrictModel):
    source_sequence: int = Field(ge=0)
    order_id: str
    trade_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    filled_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    cash_delta_fen: int
    cash_fen: int = Field(ge=0)
    positions: dict[str, int]


class VnpyOrdinaryRejection(_StrictModel):
    source_sequence: int = Field(ge=0)
    order_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    requested_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    reason: Literal[
        "INSUFFICIENT_CASH",
        "NO_POSITION",
        "INSUFFICIENT_POSITION",
    ]
    cash_fen: int = Field(ge=0)
    positions: dict[str, int]


class VnpyOrdinaryDailyAccount(_StrictModel):
    source_sequence: int = Field(ge=0)
    trade_date: date
    cash_fen: int = Field(ge=0)
    positions: dict[str, int]
    mark_prices_fen: dict[str, int]
    market_value_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_account(self) -> "VnpyOrdinaryDailyAccount":
        if set(self.positions) != set(self.mark_prices_fen):
            raise ValueError("ordinary daily marks do not cover positions")
        expected_market_value = sum(
            quantity * self.mark_prices_fen[symbol]
            for symbol, quantity in self.positions.items()
        )
        if self.market_value_fen != expected_market_value:
            raise ValueError("ordinary daily market value is invalid")
        if self.equity_fen != self.cash_fen + self.market_value_fen:
            raise ValueError("ordinary daily equity is invalid")
        return self


class VnpyOrdinaryEventReceipt(_StrictModel):
    kind: Literal["order", "trade", "mark"]
    source_sequence: int = Field(ge=0)
    object_id: str


class VnpyOrdinaryReplayResult(_StrictModel):
    operation_schema: Literal[
        "vibe.vnpy-ordinary-replay-result.v1"
    ] = "vibe.vnpy-ordinary-replay-result.v1"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_request_id: str
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qe5_backtest_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qe5_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinary_replay_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_cash_fen: int = Field(ge=0)
    orders: tuple[VnpyOrdinaryOrder, ...]
    fills: tuple[VnpyOrdinaryFill, ...]
    rejections: tuple[VnpyOrdinaryRejection, ...]
    daily_accounts: tuple[VnpyOrdinaryDailyAccount, ...] = Field(min_length=1)
    event_receipts: tuple[VnpyOrdinaryEventReceipt, ...]
    engine_version: str
    source_sha256: dict[str, str]


class VnpyChinaAReceipt(_StrictModel):
    sequence: int = Field(ge=1)
    event: str


class VnpyChinaAReplayResult(_StrictModel):
    operation_schema: Literal["vibe.vnpy-china-a-replay-result.v1"]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_request_id: str
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qe5_backtest_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qe5_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    china_a_replay_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciled_entries: int = Field(ge=1)
    receipts: tuple[VnpyChinaAReceipt, ...]
    entries: tuple[CnEquityLedgerEntry, ...]
    engine_version: str
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_receipts(self) -> "VnpyChinaAReplayResult":
        if self.reconciled_entries != len(self.receipts) or self.reconciled_entries != len(
            self.entries
        ):
            raise ValueError("China-A receipt count mismatch")
        if tuple(item.sequence for item in self.receipts) != tuple(
            range(1, len(self.receipts) + 1)
        ):
            raise ValueError("China-A receipts are incomplete or unordered")
        if tuple(item.sequence for item in self.entries) != tuple(
            range(1, len(self.entries) + 1)
        ):
            raise ValueError("China-A entries are incomplete or unordered")
        if any(
            receipt.event != entry.event
            for receipt, entry in zip(self.receipts, self.entries)
        ):
            raise ValueError("China-A receipts do not match replay entries")
        return self


@dataclass(frozen=True)
class VnpyEventReplay:
    """Validated QE6-1 EventEngine identity receipt."""

    worker: VnpyEventPathResult
    qe5_engine_request: EngineRequest


@dataclass(frozen=True)
class VnpyOrdinaryReplay:
    """Validated QE6-2 ordinary ledger reconciliation."""

    worker: VnpyOrdinaryReplayResult
    qe5: QuantaxisBacktestResult
    artifact: ReconciliationArtifact
    decision: StrategyValidationDecision


@dataclass(frozen=True)
class VnpyChinaAReplay:
    worker: VnpyChinaAReplayResult
    qe5: QuantaxisBacktestResult
    artifact: ReconciliationArtifact
    decision: StrategyValidationDecision


def _account_observation(
    *,
    cash_fen: int,
    dividend_receivable_fen: int,
    positions: Mapping[str, int],
    mark_prices_fen: Mapping[str, int],
    sellable_positions: Mapping[str, int] | None = None,
) -> ReconciliationAccountState:
    normalized_positions = dict(positions)
    normalized_marks = dict(mark_prices_fen)
    market_value_fen = sum(
        quantity * normalized_marks[symbol]
        for symbol, quantity in normalized_positions.items()
    )
    return ReconciliationAccountState(
        cash_fen=cash_fen,
        dividend_receivable_fen=dividend_receivable_fen,
        positions=normalized_positions,
        sellable_positions=dict(sellable_positions or {}),
        mark_prices_fen=normalized_marks,
        market_value_fen=market_value_fen,
        equity_fen=cash_fen + dividend_receivable_fen + market_value_fen,
    )


def _ledger_observation(entry: CnEquityLedgerEntry) -> ReconciliationObservation:
    transition = entry.model_dump(
        mode="json",
        include={
            "sequence",
            "trade_date",
            "event",
            "outcome",
            "reason",
            "order_id",
            "symbol",
            "side",
            "requested_shares",
            "filled_shares",
            "price_fen",
            "position_delta",
            "cash_delta_fen",
            "dividend_receivable_delta_fen",
            "fees",
        },
    )
    return ReconciliationObservation(
        event_output=transition,
        account_state=_account_observation(
            cash_fen=entry.cash_fen,
            dividend_receivable_fen=entry.dividend_receivable_fen,
            positions=entry.positions,
            sellable_positions=entry.sellable_positions,
            mark_prices_fen=entry.mark_prices_fen,
        ),
    )


def _validate_historical_identity(
    historical: HistoricalRunIdentity,
    *,
    compilation: StrategyCompilation,
    snapshot_sha256: str,
    qe5: QuantaxisBacktestResult,
) -> None:
    expected_engine = ReconciliationEngineIdentity(
        name="quantaxis",
        version=qe5.worker.engine_version,
        commit=QUANTAXIS_ENGINE_COMMIT,
        source_sha256=qe5.worker.source_sha256,
    )
    if (
        historical.owner_scope != compilation.engine_request.owner_scope
        or historical.engine_request_sha256
        != compilation.engine_request.content_sha256
        or historical.execution_plan_sha256 != compilation.plan.content_sha256
        or historical.snapshot_sha256 != snapshot_sha256
        or historical.backtest_input_sha256 != qe5.worker.backtest_input_sha256
        or historical.ledger_sha256 != qe5.ledger.content_sha256
        or historical.original_engine != expected_engine
    ):
        raise ValueError("historical BacktestRun identity does not match QE5 replay inputs")


def _persist_reconciliation(
    *,
    historical: HistoricalRunIdentity,
    compilation: StrategyCompilation,
    replay_input_sha256: str,
    vnpy_version: str,
    vnpy_source_sha256: Mapping[str, str],
    checkpoints: tuple[ReconciliationCheckpoint, ...],
    evidence_store: ReconciliationStore,
) -> tuple[ReconciliationArtifact, StrategyValidationDecision]:
    request = compilation.engine_request.payload
    assert isinstance(request, EngineRequest)
    artifact = create_reconciliation_artifact(
        owner_scope=historical.owner_scope,
        stream_id=historical.strategy_stream_id,
        strategy_version_id=historical.strategy_version_id,
        engine_request_id=request.request_id,
        engine_request_sha256=historical.engine_request_sha256,
        execution_plan_sha256=historical.execution_plan_sha256,
        snapshot_sha256=historical.snapshot_sha256,
        qe5_backtest_input_sha256=historical.backtest_input_sha256,
        qe5_ledger_sha256=historical.ledger_sha256,
        vnpy_replay_input_sha256=replay_input_sha256,
        qe5_engine=historical.original_engine,
        vnpy_engine=ReconciliationEngineIdentity(
            name="vnpy",
            version=vnpy_version,
            commit=VNPY_ENGINE_COMMIT,
            source_sha256=dict(vnpy_source_sha256),
        ),
        checkpoints=checkpoints,
    )
    evidence_store.put_artifact(artifact)
    stored_artifact = evidence_store.get_artifact(
        artifact.artifact_id,
        owner_scope=artifact.owner_scope,
    )
    if stored_artifact is None:
        raise ValueError("reconciliation artifact was not persisted")
    decision = decide_strategy_validation(
        stored_artifact,
        owner_scope=historical.owner_scope,
        stream_id=historical.strategy_stream_id,
        strategy_version_id=historical.strategy_version_id,
    )
    evidence_store.put_decision(decision)
    stored_decision = evidence_store.get_decision(
        decision.decision_id,
        owner_scope=decision.owner_scope,
    )
    if stored_decision is None:
        raise ValueError("strategy validation decision was not persisted")
    return stored_artifact, stored_decision


def _model_json(value: BaseModel | None) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if value is not None else None


def _ordinary_checkpoints(
    *,
    replay_events: list[dict[str, Any]],
    ledger_entries: list[CnEquityLedgerEntry],
    expected_orders: list[VnpyOrdinaryOrder],
    expected_fills: list[VnpyOrdinaryFill],
    expected_rejections: list[VnpyOrdinaryRejection],
    expected_accounts: list[VnpyOrdinaryDailyAccount],
    expected_receipts: list[VnpyOrdinaryEventReceipt],
    result: VnpyOrdinaryReplayResult,
    rule_version: str,
) -> tuple[ReconciliationCheckpoint, ...]:
    order_index = fill_index = rejection_index = account_index = receipt_index = 0
    actual_cash = result.initial_cash_fen
    actual_positions: dict[str, int] = {}
    actual_marks: dict[str, int] = {}
    checkpoints: list[ReconciliationCheckpoint] = []

    for sequence, (event_input, ledger_entry) in enumerate(
        zip(replay_events, ledger_entries),
        start=1,
    ):
        expected_output: dict[str, Any]
        actual_output: dict[str, Any]
        if event_input["event"] == "order":
            expected_order = expected_orders[order_index]
            actual_order = (
                result.orders[order_index]
                if order_index < len(result.orders)
                else None
            )
            order_index += 1
            expected_output = {"order": _model_json(expected_order)}
            actual_output = {"order": _model_json(actual_order)}
            if expected_order.status == "filled":
                expected_fill = expected_fills[fill_index]
                actual_fill = (
                    result.fills[fill_index]
                    if fill_index < len(result.fills)
                    else None
                )
                fill_index += 1
                expected_output["fill"] = _model_json(expected_fill)
                actual_output["fill"] = _model_json(actual_fill)
                if actual_fill is not None:
                    actual_cash = actual_fill.cash_fen
                    actual_positions = dict(actual_fill.positions)
                    actual_marks = {
                        symbol: price
                        for symbol, price in actual_marks.items()
                        if symbol in actual_positions
                    }
                    if actual_fill.symbol in actual_positions:
                        actual_marks[actual_fill.symbol] = actual_fill.price_fen
                receipt_count = 2
            else:
                expected_rejection = expected_rejections[rejection_index]
                actual_rejection = (
                    result.rejections[rejection_index]
                    if rejection_index < len(result.rejections)
                    else None
                )
                rejection_index += 1
                expected_output["rejection"] = _model_json(expected_rejection)
                actual_output["rejection"] = _model_json(actual_rejection)
                if actual_rejection is not None:
                    actual_cash = actual_rejection.cash_fen
                    actual_positions = dict(actual_rejection.positions)
                    actual_marks = {
                        symbol: price
                        for symbol, price in actual_marks.items()
                        if symbol in actual_positions
                    }
                receipt_count = 1
        else:
            expected_account = expected_accounts[account_index]
            actual_account = (
                result.daily_accounts[account_index]
                if account_index < len(result.daily_accounts)
                else None
            )
            account_index += 1
            expected_output = {"daily_account": _model_json(expected_account)}
            actual_output = {"daily_account": _model_json(actual_account)}
            if actual_account is not None:
                actual_cash = actual_account.cash_fen
                actual_positions = dict(actual_account.positions)
                actual_marks = dict(actual_account.mark_prices_fen)
            receipt_count = 1

        expected_event_receipts = expected_receipts[
            receipt_index : receipt_index + receipt_count
        ]
        actual_event_receipts = result.event_receipts[
            receipt_index : receipt_index + receipt_count
        ]
        receipt_index += receipt_count
        expected_output["receipts"] = [
            item.model_dump(mode="json") for item in expected_event_receipts
        ]
        actual_output["receipts"] = [
            item.model_dump(mode="json") for item in actual_event_receipts
        ]
        expected_state = _account_observation(
            cash_fen=ledger_entry.cash_fen,
            dividend_receivable_fen=0,
            positions=ledger_entry.positions,
            mark_prices_fen=ledger_entry.mark_prices_fen,
        )
        actual_state = _account_observation(
            cash_fen=actual_cash,
            dividend_receivable_fen=0,
            positions=actual_positions,
            mark_prices_fen=actual_marks,
        )
        checkpoints.append(
            ReconciliationCheckpoint(
                sequence=sequence,
                trade_date=ledger_entry.trade_date,
                event=ledger_entry.event,
                event_input=event_input,
                event_input_sha256=canonical_sha256(event_input),
                rule_version=rule_version,
                qe5=ReconciliationObservation(
                    event_output=expected_output,
                    account_state=expected_state,
                ),
                vnpy=ReconciliationObservation(
                    event_output=actual_output,
                    account_state=actual_state,
                ),
            )
        )

    unconsumed = {
        "orders": [item.model_dump(mode="json") for item in result.orders[order_index:]],
        "fills": [item.model_dump(mode="json") for item in result.fills[fill_index:]],
        "rejections": [
            item.model_dump(mode="json")
            for item in result.rejections[rejection_index:]
        ],
        "daily_accounts": [
            item.model_dump(mode="json")
            for item in result.daily_accounts[account_index:]
        ],
        "event_receipts": [
            item.model_dump(mode="json")
            for item in result.event_receipts[receipt_index:]
        ],
    }
    if any(unconsumed.values()):
        previous = checkpoints[-1]
        actual_output = dict(previous.vnpy.event_output)
        actual_output["unconsumed_result"] = unconsumed
        checkpoints[-1] = ReconciliationCheckpoint(
            sequence=previous.sequence,
            trade_date=previous.trade_date,
            event=previous.event,
            event_input=previous.event_input,
            event_input_sha256=previous.event_input_sha256,
            rule_version=previous.rule_version,
            qe5=previous.qe5,
            vnpy=ReconciliationObservation(
                event_output=actual_output,
                account_state=previous.vnpy.account_state,
            ),
        )
    return tuple(checkpoints)


def _china_a_checkpoints(
    *,
    events: tuple[Any, ...],
    expected_entries: tuple[CnEquityLedgerEntry, ...],
    actual_entries: tuple[CnEquityLedgerEntry, ...],
    rule_version: str,
) -> tuple[ReconciliationCheckpoint, ...]:
    if len(expected_entries) != len(events) or len(actual_entries) != len(events):
        raise ValueError("China-A replay did not return one entry per input event")
    return tuple(
        ReconciliationCheckpoint(
            sequence=sequence,
            trade_date=expected.trade_date,
            event=expected.event,
            event_input=event.model_dump(mode="json"),
            event_input_sha256=canonical_sha256(event),
            rule_version=rule_version,
            qe5=_ledger_observation(expected),
            vnpy=_ledger_observation(actual),
        )
        for sequence, (event, expected, actual) in enumerate(
            zip(events, expected_entries, actual_entries),
            start=1,
        )
    )


class VnpyOperationError(RuntimeError):
    """Stable error returned by an unsuccessful vn.py worker call."""

    def __init__(self, code: str, message: str, *, run: RunResult) -> None:
        super().__init__(message)
        self.code = code
        self.run = run


class VnpyOracleAdapter:
    """Map one exact QE5 compilation into the independent vn.py worker."""

    def __init__(self, runner: WorkerRunner) -> None:
        if runner.config.engine.name != "vnpy":
            raise ValueError("VnpyOracleAdapter requires the vnpy engine")
        if runner.config.engine.commit != VNPY_ENGINE_COMMIT:
            raise ValueError("VnpyOracleAdapter requires the audited vn.py commit")
        self.runner = runner

    def _run(
        self,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, Any],
        snapshot_path: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        memory_bytes: int,
    ) -> Mapping[str, Any]:
        path = snapshot_path.absolute()
        snapshot_sha256 = compute_snapshot_sha256(path)
        run = self.runner.run(
            request_id=request_id,
            operation=operation,
            payload=payload,
            snapshot_path=str(path),
            snapshot_sha256=snapshot_sha256,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            memory_bytes=memory_bytes,
            max_open_files=256,
        )
        response = run.response
        if response["status"] != "ok":
            error = response["error"]
            raise VnpyOperationError(error["code"], error["message"], run=run)
        result = response["result"]
        if result.get("snapshot_sha256") != snapshot_sha256:
            raise VnpyOperationError(
                "SNAPSHOT_RESULT_MISMATCH",
                "vn.py result did not bind the input snapshot hash",
                run=run,
            )
        return result

    def replay_event_path(
        self,
        *,
        compilation: StrategyCompilation,
        snapshot: ResearchObject,
        snapshot_path: Path,
    ) -> VnpyEventReplay:
        """Replay the exact QE5 identities without generating a second ledger."""

        request = compilation.engine_request.payload
        if not isinstance(request, EngineRequest):
            raise TypeError("compilation does not contain EngineRequest")
        if (
            request.engine.name != "quantaxis"
            or request.engine.commit != QUANTAXIS_ENGINE_COMMIT
        ):
            raise ValueError(
                "QE6-1 requires the exact audited QE5 QUANTAXIS EngineRequest"
            )
        if snapshot.ref() != compilation.plan.data_snapshot_ref:
            raise ValueError("execution plan does not reference the supplied snapshot")
        if not isinstance(snapshot.payload, DataSnapshotRef):
            raise TypeError("snapshot must contain DataSnapshotRef")
        actual_snapshot_sha256 = compute_snapshot_sha256(snapshot_path.absolute())
        if snapshot.payload.snapshot_sha256 != actual_snapshot_sha256:
            raise ValueError("DataSnapshotRef does not bind the supplied snapshot bytes")
        worker_payload = {
            "schema_version": "vibe.vnpy-event-path-request.v1",
            "engine_request": request.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
        }
        raw = self._run(
            request_id=f"qe6:{request.request_id}",
            operation="event_replay",
            payload=worker_payload,
            snapshot_path=snapshot_path,
            timeout_seconds=request.resource_limits.timeout_seconds,
            max_stdout_bytes=request.resource_limits.max_stdout_bytes,
            max_stderr_bytes=request.resource_limits.max_stderr_bytes,
            memory_bytes=request.resource_limits.memory_bytes,
        )
        result = VnpyEventPathResult.model_validate(raw)
        expected_events = (
            VnpyIdentityEvent(
                kind="engine_request",
                payload_sha256=canonical_sha256(request),
            ),
            VnpyIdentityEvent(
                kind="execution_plan",
                payload_sha256=compilation.plan.content_sha256,
            ),
            VnpyIdentityEvent(
                kind="data_snapshot",
                payload_sha256=canonical_sha256(snapshot.payload),
            ),
        )
        if (
            result.snapshot_sha256 != actual_snapshot_sha256
            or result.engine_request_id != request.request_id
            or result.engine_request_sha256 != canonical_sha256(request)
            or result.execution_plan_sha256 != compilation.plan.content_sha256
            or result.event_path_input_sha256 != canonical_sha256(worker_payload)
            or result.events != expected_events
        ):
            raise ValueError("vn.py event path identity does not match QE5 inputs")
        if (
            result.engine_version != VNPY_ENGINE_VERSION
            or result.source_sha256 != VNPY_SOURCE_SHA256
        ):
            raise ValueError("worker engine provenance does not match audited vn.py")
        return VnpyEventReplay(worker=result, qe5_engine_request=request)

    def reconcile_ordinary(
        self,
        *,
        compilation: StrategyCompilation,
        snapshot: ResearchObject,
        snapshot_path: Path,
        qe5: QuantaxisBacktestResult,
        historical_run: HistoricalRunIdentity,
        evidence_store: ReconciliationStore,
    ) -> VnpyOrdinaryReplay:
        """Replay QE5 ordinary order intents and compare every account field."""

        request = compilation.engine_request.payload
        if not isinstance(request, EngineRequest):
            raise TypeError("compilation does not contain EngineRequest")
        if (
            request.engine.name != "quantaxis"
            or request.engine.commit != QUANTAXIS_ENGINE_COMMIT
        ):
            raise ValueError(
                "QE6-2 requires the exact audited QE5 QUANTAXIS EngineRequest"
            )
        if snapshot.ref() != compilation.plan.data_snapshot_ref:
            raise ValueError("execution plan does not reference the supplied snapshot")
        if not isinstance(snapshot.payload, DataSnapshotRef):
            raise TypeError("snapshot must contain DataSnapshotRef")
        actual_snapshot_sha256 = compute_snapshot_sha256(snapshot_path.absolute())
        if snapshot.payload.snapshot_sha256 != actual_snapshot_sha256:
            raise ValueError("DataSnapshotRef does not bind the supplied snapshot bytes")
        worker = qe5.worker
        ledger = qe5.ledger
        if (
            worker.engine_request_id != request.request_id
            or worker.execution_plan_sha256 != compilation.plan.content_sha256
            or worker.snapshot_sha256 != actual_snapshot_sha256
            or ledger.data_snapshot_sha256 != actual_snapshot_sha256
            or ledger.content_sha256 != canonical_sha256(ledger)
        ):
            raise ValueError("QE5 result identity does not match QE6-2 inputs")
        _validate_historical_identity(
            historical_run,
            compilation=compilation,
            snapshot_sha256=actual_snapshot_sha256,
            qe5=qe5,
        )
        if (
            worker.engine_version != QUANTAXIS_ENGINE_VERSION
            or worker.source_sha256 != QUANTAXIS_SOURCE_SHA256
        ):
            raise ValueError("QE5 result does not use audited QUANTAXIS provenance")
        fee_schedule = worker.fee_schedule
        confirmed_costs = compilation.plan.strategy.costs
        if (
            confirmed_costs.commission_bps
            or confirmed_costs.minimum_commission
            or confirmed_costs.sell_tax_bps
            or confirmed_costs.transfer_fee_bps
            or confirmed_costs.slippage_bps
            or fee_schedule.commission_tenths_bps
            or fee_schedule.minimum_commission_fen
            or fee_schedule.sell_tax_tenths_bps
            or fee_schedule.transfer_fee_tenths_bps
            or worker.slippage_tenths_bps
        ):
            raise ValueError("QE6-2 ordinary replay excludes fees and slippage")
        if fee_schedule.rule_version != confirmed_costs.rule_version:
            raise ValueError("QE5 fee rule version does not match confirmed strategy")
        if ledger.rules != fee_schedule:
            raise ValueError("QE5 ledger fee schedule does not match worker output")
        if (
            ledger.entries[0].cash_fen != worker.initial_cash_fen
            or ledger.entries[0].trade_date != worker.opening_date
        ):
            raise ValueError("QE5 opening account does not match worker output")

        order_events = {
            item.order.order_id: item.order
            for item in worker.events
            if item.event == "order" and item.order is not None
        }
        if len(order_events) != sum(item.event == "order" for item in worker.events):
            raise ValueError("QE5 ordinary order IDs must be unique")
        mark_events = tuple(
            item for item in worker.events if item.event == "mark"
        )
        mark_index = 0
        replay_events: list[dict[str, Any]] = []
        expected_orders: list[VnpyOrdinaryOrder] = []
        expected_fills: list[VnpyOrdinaryFill] = []
        expected_rejections: list[VnpyOrdinaryRejection] = []
        expected_accounts: list[VnpyOrdinaryDailyAccount] = []
        expected_receipts: list[VnpyOrdinaryEventReceipt] = []
        checkpoint_entries: list[CnEquityLedgerEntry] = []
        seen_order_ids: set[str] = set()
        supported_rejections = {
            "INSUFFICIENT_CASH",
            "NO_POSITION",
            "INSUFFICIENT_POSITION",
        }
        for entry in ledger.entries[1:]:
            if entry.dividend_receivable_fen or entry.dividend_receivable_delta_fen:
                raise ValueError("QE6-2 ordinary replay excludes dividends")
            if entry.event in {"buy", "sell", "rejected_order"}:
                assert entry.order_id is not None
                assert entry.symbol is not None
                assert entry.side is not None
                assert entry.price_fen is not None
                order = order_events.get(entry.order_id)
                if (
                    order is None
                    or order.trade_date != entry.trade_date
                    or order.symbol != entry.symbol
                    or order.side != entry.side
                    or order.requested_shares != entry.requested_shares
                    or order.price_fen != entry.price_fen
                    or order.market_state != "open"
                    or order.maximum_fill_shares is not None
                    or entry.order_id in seen_order_ids
                ):
                    raise ValueError("QE5 ordinary order provenance is incomplete")
                seen_order_ids.add(entry.order_id)
                if entry.fees.total_fen:
                    raise ValueError("QE6-2 ordinary replay excludes fees")
                if entry.event in {"buy", "sell"}:
                    if (
                        entry.outcome != "filled"
                        or entry.reason is not None
                        or entry.filled_shares != entry.requested_shares
                    ):
                        raise ValueError(
                            "QE6-2 ordinary replay excludes partial or normalized fills"
                        )
                    reason = None
                    status: Literal["filled", "rejected"] = "filled"
                else:
                    if entry.reason not in supported_rejections:
                        raise ValueError(
                            "QE6-2 ordinary replay excludes special-rule rejections"
                        )
                    reason = entry.reason
                    status = "rejected"
                replay_events.append(
                    {
                        "source_sequence": entry.sequence,
                        "event": "order",
                        "trade_date": entry.trade_date.isoformat(),
                        "order": {
                            "order_id": entry.order_id,
                            "symbol": entry.symbol,
                            "side": entry.side,
                            "requested_shares": entry.requested_shares,
                            "price_fen": entry.price_fen,
                        },
                        "mark_prices_fen": None,
                    }
                )
                checkpoint_entries.append(entry)
                expected_orders.append(
                    VnpyOrdinaryOrder(
                        source_sequence=entry.sequence,
                        order_id=entry.order_id,
                        trade_date=entry.trade_date,
                        symbol=entry.symbol,
                        side=entry.side,
                        requested_shares=entry.requested_shares,
                        price_fen=entry.price_fen,
                        status=status,
                        reason=reason,
                    )
                )
                expected_receipts.append(
                    VnpyOrdinaryEventReceipt(
                        kind="order",
                        source_sequence=entry.sequence,
                        object_id=entry.order_id,
                    )
                )
                if status == "filled":
                    expected_fills.append(
                        VnpyOrdinaryFill(
                            source_sequence=entry.sequence,
                            order_id=entry.order_id,
                            trade_id=f"qe6-{entry.order_id}",
                            trade_date=entry.trade_date,
                            symbol=entry.symbol,
                            side=entry.side,
                            filled_shares=entry.filled_shares,
                            price_fen=entry.price_fen,
                            cash_delta_fen=entry.cash_delta_fen,
                            cash_fen=entry.cash_fen,
                            positions=entry.positions,
                        )
                    )
                    expected_receipts.append(
                        VnpyOrdinaryEventReceipt(
                            kind="trade",
                            source_sequence=entry.sequence,
                            object_id=f"qe6-{entry.order_id}",
                        )
                    )
                else:
                    expected_rejections.append(
                        VnpyOrdinaryRejection(
                            source_sequence=entry.sequence,
                            order_id=entry.order_id,
                            trade_date=entry.trade_date,
                            symbol=entry.symbol,
                            side=entry.side,
                            requested_shares=entry.requested_shares,
                            price_fen=entry.price_fen,
                            reason=reason,
                            cash_fen=entry.cash_fen,
                            positions=entry.positions,
                        )
                    )
            elif entry.event == "mark":
                if mark_index >= len(mark_events):
                    raise ValueError("QE5 ledger has an unbound daily mark")
                worker_mark = mark_events[mark_index]
                mark_index += 1
                if (
                    worker_mark.trade_date != entry.trade_date
                    or worker_mark.mark_prices_fen != entry.mark_prices_fen
                ):
                    raise ValueError("QE5 daily mark provenance is incomplete")
                replay_events.append(
                    {
                        "source_sequence": entry.sequence,
                        "event": "mark",
                        "trade_date": entry.trade_date.isoformat(),
                        "order": None,
                        "mark_prices_fen": entry.mark_prices_fen,
                    }
                )
                checkpoint_entries.append(entry)
                expected_accounts.append(
                    VnpyOrdinaryDailyAccount(
                        source_sequence=entry.sequence,
                        trade_date=entry.trade_date,
                        cash_fen=entry.cash_fen,
                        positions=entry.positions,
                        mark_prices_fen=entry.mark_prices_fen,
                        market_value_fen=entry.market_value_fen,
                        equity_fen=entry.equity_fen,
                    )
                )
                expected_receipts.append(
                    VnpyOrdinaryEventReceipt(
                        kind="mark",
                        source_sequence=entry.sequence,
                        object_id=entry.trade_date.isoformat(),
                    )
                )
            else:
                raise ValueError(
                    "QE6-2 ordinary replay excludes corporate actions"
                )
        if seen_order_ids != set(order_events):
            raise ValueError("QE5 ledger does not cover every ordinary order intent")
        if mark_index != len(mark_events):
            raise ValueError("QE5 ledger does not cover every daily mark input")
        if not expected_accounts:
            raise ValueError("QE6-2 ordinary replay requires daily account marks")

        identity = {
            "schema_version": "vibe.vnpy-event-path-request.v1",
            "engine_request": request.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
        }
        worker_payload = {
            "schema_version": "vibe.vnpy-ordinary-replay-request.v1",
            "identity": identity,
            "qe5_backtest_input_sha256": worker.backtest_input_sha256,
            "qe5_ledger_sha256": ledger.content_sha256,
            "initial_cash_fen": worker.initial_cash_fen,
            "events": replay_events,
        }
        raw = self._run(
            request_id=f"qe6-ordinary:{request.request_id}",
            operation="ordinary_replay",
            payload=worker_payload,
            snapshot_path=snapshot_path,
            timeout_seconds=request.resource_limits.timeout_seconds,
            max_stdout_bytes=request.resource_limits.max_stdout_bytes,
            max_stderr_bytes=request.resource_limits.max_stderr_bytes,
            memory_bytes=request.resource_limits.memory_bytes,
        )
        result = VnpyOrdinaryReplayResult.model_validate(raw)
        if (
            result.snapshot_sha256 != actual_snapshot_sha256
            or result.engine_request_id != request.request_id
            or result.execution_plan_sha256 != compilation.plan.content_sha256
            or result.qe5_backtest_input_sha256 != worker.backtest_input_sha256
            or result.qe5_ledger_sha256 != ledger.content_sha256
            or result.ordinary_replay_input_sha256
            != canonical_sha256(worker_payload)
            or result.initial_cash_fen != worker.initial_cash_fen
        ):
            raise ValueError("vn.py ordinary replay identity does not match QE5")
        if (
            result.engine_version != VNPY_ENGINE_VERSION
            or result.source_sha256 != VNPY_SOURCE_SHA256
        ):
            raise ValueError("worker engine provenance does not match audited vn.py")
        checkpoints = _ordinary_checkpoints(
            replay_events=replay_events,
            ledger_entries=checkpoint_entries,
            expected_orders=expected_orders,
            expected_fills=expected_fills,
            expected_rejections=expected_rejections,
            expected_accounts=expected_accounts,
            expected_receipts=expected_receipts,
            result=result,
            rule_version=fee_schedule.rule_version,
        )
        artifact, decision = _persist_reconciliation(
            historical=historical_run,
            compilation=compilation,
            replay_input_sha256=result.ordinary_replay_input_sha256,
            vnpy_version=result.engine_version,
            vnpy_source_sha256=result.source_sha256,
            checkpoints=checkpoints,
            evidence_store=evidence_store,
        )
        return VnpyOrdinaryReplay(
            worker=result,
            qe5=qe5,
            artifact=artifact,
            decision=decision,
        )

    def reconcile_china_a(
        self,
        *,
        compilation: StrategyCompilation,
        snapshot: ResearchObject,
        snapshot_path: Path,
        qe5: QuantaxisBacktestResult,
        historical_run: HistoricalRunIdentity,
        evidence_store: ReconciliationStore,
    ) -> VnpyChinaAReplay:
        """Reconcile QE5 fees and China-A rules in an independent vn.py state."""

        request = compilation.engine_request.payload
        if not isinstance(request, EngineRequest):
            raise TypeError("compilation does not contain EngineRequest")
        if request.engine.name != "quantaxis" or request.engine.commit != QUANTAXIS_ENGINE_COMMIT:
            raise ValueError("QE6-3 requires the audited QE5 EngineRequest")
        if snapshot.ref() != compilation.plan.data_snapshot_ref or not isinstance(snapshot.payload, DataSnapshotRef):
            raise ValueError("QE6-3 snapshot identity does not match the plan")
        snapshot_sha256 = compute_snapshot_sha256(snapshot_path.absolute())
        worker, ledger = qe5.worker, qe5.ledger
        if (
            snapshot.payload.snapshot_sha256 != snapshot_sha256
            or worker.snapshot_sha256 != snapshot_sha256
            or ledger.data_snapshot_sha256 != snapshot_sha256
            or worker.engine_request_id != request.request_id
            or worker.execution_plan_sha256 != compilation.plan.content_sha256
            or worker.engine_version != QUANTAXIS_ENGINE_VERSION
            or worker.source_sha256 != QUANTAXIS_SOURCE_SHA256
            or ledger.content_sha256 != canonical_sha256(ledger)
        ):
            raise ValueError("QE5 result identity does not match QE6-3 inputs")
        _validate_historical_identity(
            historical_run,
            compilation=compilation,
            snapshot_sha256=snapshot_sha256,
            qe5=qe5,
        )
        identity = {
            "schema_version": "vibe.vnpy-event-path-request.v1",
            "engine_request": request.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
        }
        payload = {
            "schema_version": "vibe.vnpy-china-a-replay-request.v1",
            "identity": identity,
            "qe5_backtest_input_sha256": worker.backtest_input_sha256,
            "qe5_ledger_sha256": ledger.content_sha256,
            "ledger": ledger.model_dump(mode="json"),
            "events": [item.model_dump(mode="json") for item in worker.events],
        }
        raw = self._run(
            request_id=f"qe6-china-a:{request.request_id}",
            operation="china_a_replay",
            payload=payload,
            snapshot_path=snapshot_path,
            timeout_seconds=request.resource_limits.timeout_seconds,
            max_stdout_bytes=request.resource_limits.max_stdout_bytes,
            max_stderr_bytes=request.resource_limits.max_stderr_bytes,
            memory_bytes=request.resource_limits.memory_bytes,
        )
        result = VnpyChinaAReplayResult.model_validate(raw)
        if (
            result.snapshot_sha256 != snapshot_sha256
            or result.engine_request_id != request.request_id
            or result.execution_plan_sha256 != compilation.plan.content_sha256
            or result.qe5_backtest_input_sha256 != worker.backtest_input_sha256
            or result.qe5_ledger_sha256 != ledger.content_sha256
            or result.china_a_replay_input_sha256 != canonical_sha256(payload)
            or result.reconciled_entries != len(ledger.entries) - 1
        ):
            raise ValueError("vn.py China-A replay identity does not match QE5")
        if result.engine_version != VNPY_ENGINE_VERSION or result.source_sha256 != VNPY_SOURCE_SHA256:
            raise ValueError("worker engine provenance does not match audited vn.py")
        checkpoints = _china_a_checkpoints(
            events=worker.events,
            expected_entries=ledger.entries[1:],
            actual_entries=result.entries,
            rule_version=worker.rule_table_version,
        )
        artifact, decision = _persist_reconciliation(
            historical=historical_run,
            compilation=compilation,
            replay_input_sha256=result.china_a_replay_input_sha256,
            vnpy_version=result.engine_version,
            vnpy_source_sha256=result.source_sha256,
            checkpoints=checkpoints,
            evidence_store=evidence_store,
        )
        return VnpyChinaAReplay(
            worker=result,
            qe5=qe5,
            artifact=artifact,
            decision=decision,
        )
