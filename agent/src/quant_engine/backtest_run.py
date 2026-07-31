"""QE5-3 normalized backtest records and content-addressed persistence."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.research.contracts import (
    BacktestMetrics,
    BacktestRun,
    DataSnapshotRef,
    EngineRequest,
    EngineIdentitySpec,
    ObjectRef,
    ResearchObject,
    canonical_json,
    canonical_sha256,
    create_research_object,
)
from src.research.store import QuotaExceededError, ResearchStore, StoreIntegrityError
from src.strategy_spec.compiler import StrategyCompilation
from src.strategy_spec.versioning import (
    StrategyConfirmationCard,
    StrategyConfirmationReceipt,
    StrategyVersion,
)

from .cn_equity_accounting import (
    CnEquityFeeBreakdown,
    CnEquityFeeSchedule,
    CnEquityLedger,
)
from .quantaxis_adapter import (
    QUANTAXIS_ENGINE_COMMIT,
    QUANTAXIS_ENGINE_VERSION,
    QUANTAXIS_SOURCE_SHA256,
    QuantaxisBacktestResult,
    QuantaxisRiskPolicy,
)


BACKTEST_RECORD_SCHEMA = "vibe.normalized-backtest-record.v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^backtest-record:[0-9a-f]{64}$"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_STRATEGY_VERSION_PATTERN = r"^strategy-version:[0-9a-f]{64}$"


class BacktestRunError(ValueError):
    """Base error for normalization and persistence failures."""


class BacktestRunIntegrityError(BacktestRunError):
    """Persisted run content, identity, or lineage is inconsistent."""


class BacktestRunIdempotencyConflict(BacktestRunError):
    """One idempotency key was reused for another semantic run."""


class BacktestRunQuotaExceeded(BacktestRunError):
    """A staged or committed run would exceed the governed artifact quota."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestRunProvenance(_StrictModel):
    """Complete reproducibility inputs for one normalized run."""

    engine_request_ref: ObjectRef
    strategy_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    strategy_stream_id: str = Field(pattern=_IDEMPOTENCY_PATTERN)
    strategy_version_id: str = Field(pattern=_STRATEGY_VERSION_PATTERN)
    strategy_version_number: int = Field(ge=1)
    parent_strategy_version_id: str | None = Field(
        default=None,
        pattern=_STRATEGY_VERSION_PATTERN,
    )
    confirmation_card_id: str
    confirmation_receipt_id: str
    confirmation_hash: str = Field(pattern=_SHA256_PATTERN)
    execution_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    backtest_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_adjustment: Literal["raw", "qfq", "hfq"]
    requested_sources: tuple[str, ...] = Field(min_length=1)
    actual_sources: dict[str, str]
    engine: EngineIdentitySpec
    engine_version: str
    engine_source_sha256: dict[str, str]
    random_seed: int = Field(ge=0, le=2_147_483_647)
    rule_table_version: str
    fee_schedule: CnEquityFeeSchedule
    slippage_tenths_bps: int = Field(ge=0)
    risk_policy: QuantaxisRiskPolicy
    initial_cash_fen: int = Field(gt=0)
    turnover_basis: Literal["gross_fill_notional_over_initial_cash"] = (
        "gross_fill_notional_over_initial_cash"
    )

    @field_validator("engine_source_sha256")
    @classmethod
    def validate_source_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(not re.fullmatch(_SHA256_PATTERN, item) for item in value.values()):
            raise ValueError("engine source hashes must be complete lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def validate_refs(self) -> "BacktestRunProvenance":
        if self.engine_request_ref.object_type != "engine_request":
            raise ValueError("engine_request_ref has the wrong object type")
        if self.strategy_spec_ref.object_type != "strategy_spec":
            raise ValueError("strategy_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        if (
            len(set(self.requested_sources)) != len(self.requested_sources)
            or any(not source for source in self.requested_sources)
        ):
            raise ValueError("requested_sources must be non-empty and unique")
        if not self.actual_sources or any(
            not source for source in self.actual_sources.values()
        ):
            raise ValueError("actual_sources must be non-empty")
        return self


class NormalizedBacktestFill(_StrictModel):
    sequence: int = Field(ge=0)
    order_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    outcome: Literal["filled", "partially_filled"]
    reason: str | None = None
    requested_shares: int = Field(gt=0)
    filled_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    notional_fen: int = Field(gt=0)
    market_state: str
    market_rule_id: str
    fees: CnEquityFeeBreakdown
    cash_delta_fen: int
    positions_after: dict[str, int]

    @model_validator(mode="after")
    def validate_fill(self) -> "NormalizedBacktestFill":
        if self.notional_fen != self.filled_shares * self.price_fen:
            raise ValueError("fill notional does not match shares and price")
        if self.outcome == "filled" and self.reason is not None:
            raise ValueError("fully filled record cannot have a partial-fill reason")
        if self.outcome == "partially_filled" and self.reason is None:
            raise ValueError("partial fill requires a reason")
        return self


class NormalizedBacktestRejection(_StrictModel):
    sequence: int = Field(ge=0)
    order_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    requested_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    market_state: str
    market_rule_id: str
    reason: str
    cash_fen: int = Field(ge=0)
    positions: dict[str, int]


class NormalizedBacktestCashEntry(_StrictModel):
    sequence: int = Field(ge=0)
    trade_date: date
    event: str
    cash_delta_fen: int
    dividend_receivable_delta_fen: int
    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)


class NormalizedBacktestPositionSnapshot(_StrictModel):
    trade_date: date
    positions: dict[str, int]
    sellable_positions: dict[str, int]
    mark_prices_fen: dict[str, int]
    market_value_fen: int = Field(ge=0)


class NormalizedBacktestFeeEntry(_StrictModel):
    sequence: int = Field(ge=0)
    order_id: str
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    fees: CnEquityFeeBreakdown


class NormalizedBacktestDailyEquity(_StrictModel):
    trade_date: date
    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)
    market_value_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_equity(self) -> "NormalizedBacktestDailyEquity":
        if self.equity_fen != (
            self.cash_fen
            + self.dividend_receivable_fen
            + self.market_value_fen
        ):
            raise ValueError("daily equity does not satisfy the accounting identity")
        return self


class NormalizedBacktestDiagnostic(_StrictModel):
    sequence: int = Field(ge=0)
    kind: Literal["rejection", "signal", "risk"]
    trade_date: date | None = None
    code: str
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    payload_json: str

    @model_validator(mode="after")
    def validate_payload(self) -> "NormalizedBacktestDiagnostic":
        try:
            decoded = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("diagnostic payload is not JSON") from exc
        if canonical_json(decoded) != self.payload_json:
            raise ValueError("diagnostic payload must be canonical JSON")
        if canonical_sha256(decoded) != self.payload_sha256:
            raise ValueError("diagnostic payload hash does not match")
        return self


class NormalizedBacktestRecord(_StrictModel):
    """Complete immutable run artifact referenced by the QE1 BacktestRun."""

    schema_version: Literal[
        "vibe.normalized-backtest-record.v1"
    ] = BACKTEST_RECORD_SCHEMA
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    owner_scope: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    status: Literal["completed"] = "completed"
    provenance: BacktestRunProvenance
    ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    metrics: BacktestMetrics
    fills: tuple[NormalizedBacktestFill, ...]
    rejections: tuple[NormalizedBacktestRejection, ...]
    cash_entries: tuple[NormalizedBacktestCashEntry, ...]
    position_snapshots: tuple[NormalizedBacktestPositionSnapshot, ...]
    fee_entries: tuple[NormalizedBacktestFeeEntry, ...]
    daily_equity: tuple[NormalizedBacktestDailyEquity, ...] = Field(min_length=1)
    diagnostics: tuple[NormalizedBacktestDiagnostic, ...]
    ledger: CnEquityLedger

    @property
    def artifact_ref(self) -> str:
        return f"backtest-record:sha256:{self.content_sha256}"

    @model_validator(mode="after")
    def validate_record(self) -> "NormalizedBacktestRecord":
        material = self.model_dump(
            mode="json",
            exclude={"run_id", "content_sha256"},
        )
        expected = canonical_sha256(material)
        if self.content_sha256 != expected:
            raise ValueError("record content_sha256 does not match content")
        if self.run_id != f"backtest-record:{expected}":
            raise ValueError("run_id does not match record content")
        if self.ledger.content_sha256 != self.ledger_sha256:
            raise ValueError("ledger_sha256 does not match embedded ledger")
        if self.ledger.data_snapshot_sha256 != self.provenance.snapshot_sha256:
            raise ValueError("ledger and provenance snapshot hashes differ")
        if self.ledger.rules != self.provenance.fee_schedule:
            raise ValueError("ledger rules and provenance fee schedule differ")
        if self.ledger.entries[0].cash_fen != self.provenance.initial_cash_fen:
            raise ValueError("ledger opening cash and provenance differ")
        if len(self.cash_entries) != len(self.ledger.entries):
            raise ValueError("cash entries do not cover the complete ledger")
        for normalized, entry in zip(self.cash_entries, self.ledger.entries):
            if (
                normalized.sequence,
                normalized.trade_date,
                normalized.event,
                normalized.cash_delta_fen,
                normalized.dividend_receivable_delta_fen,
                normalized.cash_fen,
                normalized.dividend_receivable_fen,
            ) != (
                entry.sequence,
                entry.trade_date,
                entry.event,
                entry.cash_delta_fen,
                entry.dividend_receivable_delta_fen,
                entry.cash_fen,
                entry.dividend_receivable_fen,
            ):
                raise ValueError("cash entries do not match the embedded ledger")
        trade_entries = tuple(
            item for item in self.ledger.entries if item.event in {"buy", "sell"}
        )
        if len(self.fills) != len(trade_entries):
            raise ValueError("fills do not cover every ledger trade")
        for normalized, entry in zip(self.fills, trade_entries):
            if (
                normalized.sequence,
                normalized.order_id,
                normalized.trade_date,
                normalized.symbol,
                normalized.side,
                normalized.outcome,
                normalized.reason,
                normalized.requested_shares,
                normalized.filled_shares,
                normalized.price_fen,
                normalized.fees,
                normalized.cash_delta_fen,
                normalized.positions_after,
            ) != (
                entry.sequence,
                entry.order_id,
                entry.trade_date,
                entry.symbol,
                entry.side,
                entry.outcome,
                entry.reason,
                entry.requested_shares,
                entry.filled_shares,
                entry.price_fen,
                entry.fees,
                entry.cash_delta_fen,
                entry.positions,
            ):
                raise ValueError("fills do not match the embedded ledger")
        rejected_entries = tuple(
            item for item in self.ledger.entries if item.event == "rejected_order"
        )
        if len(self.rejections) != len(rejected_entries):
            raise ValueError("rejections do not cover every rejected ledger order")
        for normalized, entry in zip(self.rejections, rejected_entries):
            if (
                normalized.sequence,
                normalized.order_id,
                normalized.trade_date,
                normalized.symbol,
                normalized.side,
                normalized.requested_shares,
                normalized.price_fen,
                normalized.reason,
                normalized.cash_fen,
                normalized.positions,
            ) != (
                entry.sequence,
                entry.order_id,
                entry.trade_date,
                entry.symbol,
                entry.side,
                entry.requested_shares,
                entry.price_fen,
                entry.reason,
                entry.cash_fen,
                entry.positions,
            ):
                raise ValueError("rejections do not match the embedded ledger")
        expected_fees = tuple(
            (
                item.sequence,
                item.order_id,
                item.trade_date,
                item.symbol,
                item.side,
                item.fees,
            )
            for item in self.fills
        )
        actual_fees = tuple(
            (
                item.sequence,
                item.order_id,
                item.trade_date,
                item.symbol,
                item.side,
                item.fees,
            )
            for item in self.fee_entries
        )
        if actual_fees != expected_fees:
            raise ValueError("fee entries do not match normalized fills")
        mark_entries = tuple(
            item for item in self.ledger.entries if item.event == "mark"
        )
        if len(self.daily_equity) != len(mark_entries):
            raise ValueError("daily equity does not cover every ledger mark")
        if len(self.position_snapshots) != len(mark_entries):
            raise ValueError("positions do not cover every ledger mark")
        for equity, position, entry in zip(
            self.daily_equity,
            self.position_snapshots,
            mark_entries,
        ):
            if (
                equity.trade_date,
                equity.cash_fen,
                equity.dividend_receivable_fen,
                equity.market_value_fen,
                equity.equity_fen,
            ) != (
                entry.trade_date,
                entry.cash_fen,
                entry.dividend_receivable_fen,
                entry.market_value_fen,
                entry.equity_fen,
            ):
                raise ValueError("daily equity does not match ledger marks")
            if (
                position.trade_date,
                position.positions,
                position.sellable_positions,
                position.mark_prices_fen,
                position.market_value_fen,
            ) != (
                entry.trade_date,
                entry.positions,
                entry.sellable_positions,
                entry.mark_prices_fen,
                entry.market_value_fen,
            ):
                raise ValueError("positions do not match ledger marks")
        if tuple(item.sequence for item in self.diagnostics) != tuple(
            range(len(self.diagnostics))
        ):
            raise ValueError("diagnostic sequence must be contiguous")
        expected_metrics = _metrics(
            ledger=self.ledger,
            fills=self.fills,
            initial_cash_fen=self.provenance.initial_cash_fen,
        )
        if self.metrics != expected_metrics:
            raise ValueError("metrics do not match the embedded ledger")
        return self


@dataclass(frozen=True)
class PreparedBacktestRun:
    record: NormalizedBacktestRecord
    object: ResearchObject


@dataclass(frozen=True)
class PersistedBacktestRun:
    record: NormalizedBacktestRecord
    object: ResearchObject
    created: bool


class _BacktestTransaction(_StrictModel):
    transaction_id: str = Field(pattern=r"^backtest-txn:[0-9a-f]{64}$")
    owner_scope: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    staged_record_name: str = Field(
        pattern=r"^backtest-txn-[0-9a-f]{64}\.record\.json$"
    )
    summary: ResearchObject

    @model_validator(mode="after")
    def validate_transaction(self) -> "_BacktestTransaction":
        if self.summary.object_type != "backtest_run":
            raise ValueError("transaction summary must contain BacktestRun")
        return self


@dataclass(frozen=True)
class BacktestBackupResult:
    destination: Path
    run_count: int
    research_object_count: int
    manifest_sha256: str


def _diagnostic(
    *,
    sequence: int,
    kind: Literal["rejection", "signal", "risk"],
    code: str,
    payload: Mapping[str, Any],
    trade_date: date | None,
) -> NormalizedBacktestDiagnostic:
    payload_json = canonical_json(payload)
    return NormalizedBacktestDiagnostic(
        sequence=sequence,
        kind=kind,
        trade_date=trade_date,
        code=code,
        payload_sha256=canonical_sha256(payload),
        payload_json=payload_json,
    )


def _metrics(
    *,
    ledger: CnEquityLedger,
    fills: tuple[NormalizedBacktestFill, ...],
    initial_cash_fen: int,
) -> BacktestMetrics:
    if initial_cash_fen <= 0:
        raise BacktestRunError("completed BacktestRun requires positive initial cash")
    mark_entries = tuple(item for item in ledger.entries if item.event == "mark")
    if not mark_entries:
        raise BacktestRunError("completed BacktestRun requires daily mark entries")
    final_equity = mark_entries[-1].equity_fen
    total_return = final_equity / initial_cash_fen - 1.0
    peak = initial_cash_fen
    max_drawdown = 0.0
    for entry in mark_entries:
        peak = max(peak, entry.equity_fen)
        if peak:
            max_drawdown = min(
                max_drawdown,
                entry.equity_fen / peak - 1.0,
            )
    gross_notional = sum(item.notional_fen for item in fills)
    return BacktestMetrics(
        total_return=total_return,
        annualized_return=None,
        max_drawdown=max_drawdown,
        turnover=gross_notional / initial_cash_fen,
        trade_count=len(fills),
    )


def _build_summary(
    record: NormalizedBacktestRecord,
    *,
    created_at: datetime | None = None,
) -> ResearchObject:
    provenance = record.provenance
    payload = BacktestRun(
        engine_request_ref=provenance.engine_request_ref,
        strategy_spec_ref=provenance.strategy_spec_ref,
        data_snapshot_ref=provenance.data_snapshot_ref,
        engine=provenance.engine,
        status="completed",
        ledger_sha256=record.ledger_sha256,
        metrics=record.metrics,
        artifact_refs=(record.artifact_ref,),
        diagnostics=(
            "normalized_schema:vibe.normalized-backtest-record.v1",
            f"fills:{len(record.fills)}",
            f"rejections:{len(record.rejections)}",
        ),
    )
    return create_research_object(
        payload,
        owner_scope=record.owner_scope,
        parent_refs=(
            provenance.engine_request_ref,
            provenance.strategy_spec_ref,
            provenance.data_snapshot_ref,
        ),
        created_at=created_at,
    )


def normalize_quantaxis_backtest_run(
    result: QuantaxisBacktestResult,
    *,
    compilation: StrategyCompilation,
    version: StrategyVersion,
    card: StrategyConfirmationCard,
    receipt: StrategyConfirmationReceipt,
    snapshot: ResearchObject,
    created_at: datetime | None = None,
) -> PreparedBacktestRun:
    """Normalize one reconciled QE5-2 result into a content-addressed run."""

    request_object = compilation.engine_request
    request = request_object.payload
    snapshot_payload = snapshot.payload
    if not isinstance(request, EngineRequest):
        raise BacktestRunError("compilation must contain EngineRequest")
    if not isinstance(snapshot_payload, DataSnapshotRef):
        raise BacktestRunError("snapshot must contain DataSnapshotRef")
    if version.strategy is None or version.strategy_spec_ref is None:
        raise BacktestRunError("BacktestRun requires an executable strategy version")
    if (
        request_object.owner_scope != version.owner_scope
        or snapshot.owner_scope != version.owner_scope
    ):
        raise BacktestRunError("BacktestRun inputs must share owner_scope")
    if (
        request.strategy_spec_ref != version.strategy_spec_ref
        or request.data_snapshot_ref != snapshot.ref()
        or compilation.plan.strategy_spec_ref != version.strategy_spec_ref
        or compilation.plan.strategy != version.strategy
        or result.worker.execution_plan_sha256 != compilation.plan.content_sha256
        or result.worker.engine_request_id != request.request_id
        or result.worker.snapshot_sha256 != snapshot_payload.snapshot_sha256
        or receipt.version_id != version.version_id
        or card.version_id != version.version_id
        or receipt.stream_id != version.stream_id
        or card.stream_id != version.stream_id
        or receipt.confirmation_hash != card.confirmation_hash
    ):
        raise BacktestRunError("BacktestRun inputs do not share exact lineage")
    ledger = result.ledger
    if (
        request.engine.name != "quantaxis"
        or request.engine.commit != QUANTAXIS_ENGINE_COMMIT
        or result.worker.engine_version != QUANTAXIS_ENGINE_VERSION
        or result.worker.source_sha256 != QUANTAXIS_SOURCE_SHA256
        or result.worker.fee_schedule != ledger.rules
        or result.worker.board_lot != ledger.board_lot
        or result.worker.initial_cash_fen <= 0
        or ledger.entries[0].cash_fen != result.worker.initial_cash_fen
    ):
        raise BacktestRunError("BacktestRun engine or ledger provenance is invalid")
    expected_input = {
        "schema_version": "vibe.quantaxis-backtest-request.v1",
        "engine_request": request.model_dump(mode="json"),
        "execution_plan": compilation.plan.model_dump(mode="json"),
        "data_snapshot_ref": snapshot_payload.model_dump(mode="json"),
        "confirmation": {
            "version_id": version.version_id,
            "card_id": card.card_id,
            "receipt_id": receipt.receipt_id,
            "confirmation_hash": receipt.confirmation_hash,
        },
        "initial_cash_fen": result.worker.initial_cash_fen,
    }
    if result.worker.backtest_input_sha256 != canonical_sha256(expected_input):
        raise BacktestRunError("BacktestRun input identity does not match exact inputs")

    order_events = {
        event.order.order_id: event
        for event in result.worker.events
        if event.event == "order"
        and event.order is not None
        and event.market_rule_id is not None
    }
    if len(order_events) != sum(
        event.event == "order" for event in result.worker.events
    ):
        raise BacktestRunError("worker order events have incomplete provenance")
    fills: list[NormalizedBacktestFill] = []
    rejections: list[NormalizedBacktestRejection] = []
    cash_entries: list[NormalizedBacktestCashEntry] = []
    positions: list[NormalizedBacktestPositionSnapshot] = []
    fees: list[NormalizedBacktestFeeEntry] = []
    daily_equity: list[NormalizedBacktestDailyEquity] = []
    diagnostics: list[NormalizedBacktestDiagnostic] = []

    for entry in ledger.entries:
        cash_entries.append(
            NormalizedBacktestCashEntry(
                sequence=entry.sequence,
                trade_date=entry.trade_date,
                event=entry.event,
                cash_delta_fen=entry.cash_delta_fen,
                dividend_receivable_delta_fen=entry.dividend_receivable_delta_fen,
                cash_fen=entry.cash_fen,
                dividend_receivable_fen=entry.dividend_receivable_fen,
            )
        )
        if entry.event in {"buy", "sell"}:
            assert entry.order_id is not None
            assert entry.symbol is not None
            assert entry.side is not None
            assert entry.price_fen is not None
            order_event = order_events.get(entry.order_id)
            if order_event is None or order_event.market_rule_id is None:
                raise BacktestRunError("filled order is missing market-rule provenance")
            fill = NormalizedBacktestFill(
                sequence=entry.sequence,
                order_id=entry.order_id,
                trade_date=entry.trade_date,
                symbol=entry.symbol,
                side=entry.side,
                outcome=entry.outcome,
                reason=entry.reason,
                requested_shares=entry.requested_shares,
                filled_shares=entry.filled_shares,
                price_fen=entry.price_fen,
                notional_fen=entry.filled_shares * entry.price_fen,
                market_state=order_event.order.market_state,
                market_rule_id=order_event.market_rule_id,
                fees=entry.fees,
                cash_delta_fen=entry.cash_delta_fen,
                positions_after=entry.positions,
            )
            fills.append(fill)
            fees.append(
                NormalizedBacktestFeeEntry(
                    sequence=entry.sequence,
                    order_id=entry.order_id,
                    trade_date=entry.trade_date,
                    symbol=entry.symbol,
                    side=entry.side,
                    fees=entry.fees,
                )
            )
        elif entry.event == "rejected_order":
            assert entry.order_id is not None
            assert entry.symbol is not None
            assert entry.side is not None
            assert entry.price_fen is not None
            event = order_events.get(entry.order_id)
            if event is None or event.market_rule_id is None:
                raise BacktestRunError("rejected order is missing worker provenance")
            rejection = NormalizedBacktestRejection(
                sequence=entry.sequence,
                order_id=entry.order_id,
                trade_date=entry.trade_date,
                symbol=entry.symbol,
                side=entry.side,
                requested_shares=entry.requested_shares,
                price_fen=entry.price_fen,
                market_state=event.order.market_state,
                market_rule_id=event.market_rule_id,
                reason=str(entry.reason),
                cash_fen=entry.cash_fen,
                positions=entry.positions,
            )
            rejections.append(rejection)
            diagnostics.append(
                _diagnostic(
                    sequence=len(diagnostics),
                    kind="rejection",
                    code=rejection.reason,
                    payload=rejection.model_dump(mode="json"),
                    trade_date=rejection.trade_date,
                )
            )
        if entry.event == "mark":
            positions.append(
                NormalizedBacktestPositionSnapshot(
                    trade_date=entry.trade_date,
                    positions=entry.positions,
                    sellable_positions=entry.sellable_positions,
                    mark_prices_fen=entry.mark_prices_fen,
                    market_value_fen=entry.market_value_fen,
                )
            )
            daily_equity.append(
                NormalizedBacktestDailyEquity(
                    trade_date=entry.trade_date,
                    cash_fen=entry.cash_fen,
                    dividend_receivable_fen=entry.dividend_receivable_fen,
                    market_value_fen=entry.market_value_fen,
                    equity_fen=entry.equity_fen,
                )
            )

    for item in result.worker.signal_audit:
        try:
            trade_date = date.fromisoformat(str(item["trade_date"]))
        except (KeyError, ValueError) as exc:
            raise BacktestRunError("signal audit has an invalid trade_date") from exc
        diagnostics.append(
            _diagnostic(
                sequence=len(diagnostics),
                kind="signal",
                code="SIGNAL_EVALUATION",
                payload=item,
                trade_date=trade_date,
            )
        )
    for item in result.worker.risk_audit:
        diagnostics.append(
            _diagnostic(
                sequence=len(diagnostics),
                kind="risk",
                code="DRAWDOWN_HALTED" if item.drawdown_halted else "RISK_OBSERVATION",
                payload=item.model_dump(mode="json"),
                trade_date=item.trade_date,
            )
        )

    normalized_fills = tuple(fills)
    provenance = BacktestRunProvenance(
        engine_request_ref=request_object.ref(),
        strategy_spec_ref=version.strategy_spec_ref,
        data_snapshot_ref=snapshot.ref(),
        strategy_stream_id=version.stream_id,
        strategy_version_id=version.version_id,
        strategy_version_number=version.version_number,
        parent_strategy_version_id=version.parent_version_id,
        confirmation_card_id=card.card_id,
        confirmation_receipt_id=receipt.receipt_id,
        confirmation_hash=receipt.confirmation_hash,
        execution_plan_sha256=compilation.plan.content_sha256,
        backtest_input_sha256=result.worker.backtest_input_sha256,
        snapshot_sha256=snapshot_payload.snapshot_sha256,
        snapshot_adjustment=snapshot_payload.adjustment,
        requested_sources=snapshot_payload.requested_sources,
        actual_sources=snapshot_payload.actual_sources,
        engine=request.engine,
        engine_version=result.worker.engine_version,
        engine_source_sha256=result.worker.source_sha256,
        random_seed=request.random_seed,
        rule_table_version=result.worker.rule_table_version,
        fee_schedule=result.worker.fee_schedule,
        slippage_tenths_bps=result.worker.slippage_tenths_bps,
        risk_policy=result.worker.risk_policy,
        initial_cash_fen=result.worker.initial_cash_fen,
    )
    material = {
        "schema_version": BACKTEST_RECORD_SCHEMA,
        "owner_scope": version.owner_scope,
        "status": "completed",
        "provenance": provenance.model_dump(mode="json"),
        "ledger_sha256": ledger.content_sha256,
        "metrics": _metrics(
            ledger=ledger,
            fills=normalized_fills,
            initial_cash_fen=result.worker.initial_cash_fen,
        ).model_dump(mode="json"),
        "fills": [item.model_dump(mode="json") for item in normalized_fills],
        "rejections": [item.model_dump(mode="json") for item in rejections],
        "cash_entries": [item.model_dump(mode="json") for item in cash_entries],
        "position_snapshots": [item.model_dump(mode="json") for item in positions],
        "fee_entries": [item.model_dump(mode="json") for item in fees],
        "daily_equity": [item.model_dump(mode="json") for item in daily_equity],
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "ledger": ledger.model_dump(mode="json"),
    }
    digest = canonical_sha256(material)
    record = NormalizedBacktestRecord(
        **material,
        run_id=f"backtest-record:{digest}",
        content_sha256=digest,
    )
    return PreparedBacktestRun(
        record=record,
        object=_build_summary(record, created_at=created_at),
    )


class BacktestRunStore:
    """Immutable record files plus a rebuildable idempotency/lineage index."""

    def __init__(
        self,
        root: Path,
        *,
        research_store: ResearchStore,
        max_record_bytes: int = 67_108_864,
        total_quota_bytes: int = 1_073_741_824,
    ) -> None:
        if max_record_bytes <= 0 or total_quota_bytes <= 0:
            raise ValueError("BacktestRun store quotas must be positive")
        if max_record_bytes > total_quota_bytes:
            raise ValueError("max_record_bytes cannot exceed total_quota_bytes")
        self.root = Path(root)
        self.research_store = research_store
        self.max_record_bytes = max_record_bytes
        self.total_quota_bytes = total_quota_bytes
        self.records_dir = self.root / "records"
        self.transactions_dir = self.root / "transactions"
        self.database_path = self.root / "backtest-runs.db"
        self.lock_path = self.root / ".backtest-runs.lock"
        self._prepare()
        self._initialize_database()
        self.pending_recovery_count = self.recover_pending()

    def put(
        self,
        prepared: PreparedBacktestRun,
        *,
        idempotency_key: str,
    ) -> PersistedBacktestRun:
        if not re.fullmatch(_IDEMPOTENCY_PATTERN, idempotency_key):
            raise ValueError("idempotency_key is invalid")
        record = prepared.record
        expected = _build_summary(record, created_at=prepared.object.created_at)
        if expected != prepared.object:
            raise BacktestRunIntegrityError("BacktestRun summary does not match record")
        payload = (canonical_json(record) + "\n").encode("utf-8")
        if len(payload) > self.max_record_bytes:
            raise BacktestRunError("normalized BacktestRun exceeds max_record_bytes")

        with self._exclusive_lock():
            existing_idempotency = self._idempotency_row(
                record.owner_scope,
                idempotency_key,
            )
            if existing_idempotency is not None:
                if (
                    existing_idempotency["run_id"] != record.run_id
                    or existing_idempotency["request_sha256"]
                    != record.provenance.backtest_input_sha256
                ):
                    raise BacktestRunIdempotencyConflict(
                        "idempotency key was used for another BacktestRun"
                    )
                existing = self.get(
                    record.run_id,
                    owner_scope=record.owner_scope,
                )
                if existing is None:
                    raise BacktestRunIntegrityError(
                        "idempotency index references a missing BacktestRun"
                    )
                return PersistedBacktestRun(
                    record=existing.record,
                    object=existing.object,
                    created=False,
                )
            transaction = self._ensure_transaction_locked(
                prepared,
                idempotency_key=idempotency_key,
                record_payload=payload,
            )
            return self._commit_transaction_locked(transaction)

    def recover_pending(self) -> int:
        """Finish every durable transaction whose parent chain is now available."""

        recovered = 0
        with self._exclusive_lock():
            for path in self._transaction_manifest_paths():
                transaction = self._load_transaction(path)
                try:
                    self._commit_transaction_locked(transaction)
                except StoreIntegrityError as exc:
                    if "parent object is not stored" in str(exc):
                        continue
                    raise
                except QuotaExceededError:
                    continue
                recovered += 1
            self._clean_orphan_staging_locked()
        return recovered

    def quota_usage_bytes(self) -> int:
        """Return unique inode bytes charged to immutable records and staging."""

        with self._exclusive_lock():
            return self._artifact_bytes_on_disk()

    def prune_unreferenced(
        self,
        *,
        owner_scope: str,
        keep_last: int,
        created_before: datetime,
        pinned_run_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Delete old unreferenced runs while preserving newest and pinned history."""

        if keep_last < 0:
            raise ValueError("keep_last must be non-negative")
        if created_before.tzinfo is None or created_before.utcoffset() is None:
            raise ValueError("created_before must be timezone-aware")
        pinned = set(pinned_run_ids)
        for run_id in pinned:
            self._parse_run_id(run_id)
        removed: list[str] = []
        with self._exclusive_lock():
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id, owner_scope, backtest_object_id, created_at
                    FROM runs WHERE owner_scope=?
                    ORDER BY created_at DESC, run_id ASC
                    """,
                    (owner_scope,),
                ).fetchall()
            retained = {row["run_id"] for row in rows[:keep_last]} | pinned
            for row in rows[keep_last:]:
                if row["run_id"] in retained:
                    continue
                created_at = datetime.fromisoformat(row["created_at"])
                if created_at >= created_before:
                    continue
                digest = self._parse_run_id(row["run_id"])
                target = self._record_path(digest)
                record = self._load_record(target)
                summary = self.research_store.get(
                    row["backtest_object_id"],
                    owner_scope=owner_scope,
                )
                if summary is None:
                    raise BacktestRunIntegrityError(
                        "retention candidate has no matching BacktestRun summary"
                    )
                tombstone = target.with_name(f".{target.name}.delete")
                if tombstone.exists() or tombstone.is_symlink():
                    raise BacktestRunIntegrityError(
                        "BacktestRun deletion tombstone already exists"
                    )
                os.replace(target, tombstone)
                self._fsync_directory(self.records_dir)
                summary_deleted = False
                try:
                    summary_deleted = self.research_store.delete_leaf(
                        summary.object_id,
                        owner_scope=owner_scope,
                    )
                    if not summary_deleted:
                        os.replace(tombstone, target)
                        self._fsync_directory(self.records_dir)
                        continue
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "DELETE FROM idempotency_keys WHERE run_id=?",
                            (record.run_id,),
                        )
                        changed = connection.execute(
                            "DELETE FROM runs WHERE run_id=? AND owner_scope=?",
                            (record.run_id, owner_scope),
                        ).rowcount
                        if changed != 1:
                            raise BacktestRunIntegrityError(
                                "retention candidate disappeared from run index"
                            )
                        connection.commit()
                    tombstone.unlink()
                    self._fsync_directory(self.records_dir)
                    removed.append(record.run_id)
                except Exception:
                    if tombstone.exists() and not target.exists():
                        os.replace(tombstone, target)
                        self._fsync_directory(self.records_dir)
                    if summary_deleted:
                        self.research_store.put(summary)
                    raise
        return tuple(removed)

    def backup_to(self, destination: Path) -> BacktestBackupResult:
        """Create and validate one isolated ResearchStore + BacktestRunStore backup."""

        destination = Path(destination)
        self._validate_backup_destination(destination)
        research_destination = destination / "research"
        backtest_destination = destination / "backtests"

        with self._exclusive_lock():
            pending = self._transaction_manifest_paths()
            if pending:
                raise BacktestRunIntegrityError(
                    "cannot back up BacktestRun store with pending transactions"
                )
            destination.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(destination, 0o700)
            backtest_destination.mkdir(mode=0o700)
            (backtest_destination / "records").mkdir(mode=0o700)
            (backtest_destination / "transactions").mkdir(mode=0o700)
            research_count = self.research_store.backup_to(research_destination)
            run_count = 0
            for source in self._record_paths():
                record = self._load_record(source)
                target = backtest_destination / "records" / source.name
                self._atomic_write(
                    target,
                    (canonical_json(record) + "\n").encode("utf-8"),
                )
                run_count += 1
            backup_database = backtest_destination / self.database_path.name
            with self._connect() as source_connection:
                with sqlite3.connect(backup_database) as backup_connection:
                    source_connection.backup(backup_connection)
                    backup_connection.commit()
            os.chmod(backup_database, 0o600)

        manifest = {
            "schema_version": "vibe.backtest-backup.v1",
            "run_count": run_count,
            "research_object_count": research_count,
            "records": [
                {
                    "name": path.name,
                    "sha256": path.stem,
                    "size_bytes": path.stat(follow_symlinks=False).st_size,
                }
                for path in sorted((backtest_destination / "records").glob("*.json"))
            ],
        }
        manifest_sha256 = canonical_sha256(manifest)
        self._atomic_write(
            destination / "manifest.json",
            (
                canonical_json(
                    {
                        **manifest,
                        "manifest_sha256": manifest_sha256,
                    }
                )
                + "\n"
            ).encode("utf-8"),
        )
        restored_research = ResearchStore(
            research_destination,
            total_quota_bytes=self.research_store.total_quota_bytes,
            max_object_bytes=self.research_store.max_object_bytes,
        )
        restored = BacktestRunStore(
            backtest_destination,
            research_store=restored_research,
            max_record_bytes=self.max_record_bytes,
            total_quota_bytes=self.total_quota_bytes,
        )
        with restored._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, owner_scope FROM runs ORDER BY run_id"
            ).fetchall()
        if len(rows) != run_count:
            raise BacktestRunIntegrityError("backup index count does not match records")
        for row in rows:
            if restored.get(row["run_id"], owner_scope=row["owner_scope"]) is None:
                raise BacktestRunIntegrityError("backup cannot restore an indexed run")
        return BacktestBackupResult(
            destination=destination,
            run_count=run_count,
            research_object_count=research_count,
            manifest_sha256=manifest_sha256,
        )

    def _ensure_transaction_locked(
        self,
        prepared: PreparedBacktestRun,
        *,
        idempotency_key: str,
        record_payload: bytes,
    ) -> _BacktestTransaction:
        record = prepared.record
        digest = canonical_sha256(
            {
                "owner_scope": record.owner_scope,
                "idempotency_key": idempotency_key,
            }
        )
        transaction = _BacktestTransaction(
            transaction_id=f"backtest-txn:{digest}",
            owner_scope=record.owner_scope,
            idempotency_key=idempotency_key,
            request_sha256=record.provenance.backtest_input_sha256,
            record_sha256=record.content_sha256,
            staged_record_name=f"backtest-txn-{digest}.record.json",
            summary=prepared.object,
        )
        manifest_path = self._transaction_manifest_path(digest)
        staged_path = self.transactions_dir / transaction.staged_record_name
        committed_path = self._record_path(record.content_sha256)
        if manifest_path.exists() or manifest_path.is_symlink():
            existing = self._load_transaction(manifest_path)
            if existing != transaction:
                raise BacktestRunIdempotencyConflict(
                    "pending transaction belongs to another BacktestRun"
                )
        if staged_path.exists() or staged_path.is_symlink():
            if self._load_record(staged_path) != record:
                raise BacktestRunIntegrityError(
                    "pending transaction record contains different content"
                )
        elif committed_path.exists() or committed_path.is_symlink():
            if self._load_record(committed_path) != record:
                raise BacktestRunIntegrityError(
                    "existing run path contains different content"
                )
            os.link(committed_path, staged_path)
        else:
            manifest_payload = (canonical_json(transaction) + "\n").encode("utf-8")
            usage = self._artifact_bytes_on_disk()
            if usage + len(record_payload) + len(manifest_payload) > self.total_quota_bytes:
                raise BacktestRunQuotaExceeded(
                    "BacktestRun transaction would exceed total_quota_bytes"
                )
            self._atomic_write(staged_path, record_payload)
        if not manifest_path.exists():
            manifest_payload = (canonical_json(transaction) + "\n").encode("utf-8")
            usage = self._artifact_bytes_on_disk()
            if usage + len(manifest_payload) > self.total_quota_bytes:
                staged_path.unlink(missing_ok=True)
                raise BacktestRunQuotaExceeded(
                    "BacktestRun transaction would exceed total_quota_bytes"
                )
            self._atomic_write(manifest_path, manifest_payload)
        return transaction

    def _commit_transaction_locked(
        self,
        transaction: _BacktestTransaction,
    ) -> PersistedBacktestRun:
        manifest_digest = transaction.transaction_id.rsplit(":", 1)[1]
        manifest_path = self._transaction_manifest_path(manifest_digest)
        staged_path = self.transactions_dir / transaction.staged_record_name
        target = self._record_path(transaction.record_sha256)
        if not staged_path.exists() and target.exists():
            os.link(target, staged_path)
        record = self._load_record(staged_path)
        if (
            record.content_sha256 != transaction.record_sha256
            or record.owner_scope != transaction.owner_scope
            or record.provenance.backtest_input_sha256
            != transaction.request_sha256
        ):
            raise BacktestRunIntegrityError(
                "transaction record does not match its durable manifest"
            )
        expected_summary = _build_summary(
            record,
            created_at=transaction.summary.created_at,
        )
        if expected_summary != transaction.summary:
            raise BacktestRunIntegrityError(
                "transaction summary does not match its normalized record"
            )
        existing_idempotency = self._idempotency_row(
            record.owner_scope,
            transaction.idempotency_key,
        )
        if existing_idempotency is not None:
            if (
                existing_idempotency["run_id"] != record.run_id
                or existing_idempotency["request_sha256"]
                != record.provenance.backtest_input_sha256
            ):
                raise BacktestRunIdempotencyConflict(
                    "idempotency key was used for another BacktestRun"
                )
            existing = self.get(record.run_id, owner_scope=record.owner_scope)
            if existing is None:
                raise BacktestRunIntegrityError(
                    "idempotency index references a missing BacktestRun"
                )
            self._remove_transaction_files(manifest_path, staged_path)
            return existing

        record_created = False
        if target.exists() or target.is_symlink():
            if self._load_record(target) != record:
                raise BacktestRunIntegrityError(
                    "existing run path contains different content"
                )
        else:
            try:
                os.link(staged_path, target)
            except FileExistsError:
                pass
            if self._load_record(target) != record:
                raise BacktestRunIntegrityError(
                    "new run path contains different content"
                )
            self._fsync_directory(self.records_dir)
            record_created = True

        object_result = self.research_store.put(transaction.summary)
        self._index_committed_run(
            record,
            summary=object_result.object,
            idempotency_key=transaction.idempotency_key,
            target=target,
        )
        self._remove_transaction_files(manifest_path, staged_path)
        return PersistedBacktestRun(
            record=record,
            object=object_result.object,
            created=record_created or object_result.created,
        )

    def _index_committed_run(
        self,
        record: NormalizedBacktestRecord,
        *,
        summary: ResearchObject,
        idempotency_key: str,
        target: Path,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, owner_scope, strategy_stream_id,
                    strategy_version_id, parent_strategy_version_id,
                    request_sha256, record_sha256, backtest_object_id,
                    relative_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    record.run_id,
                    record.owner_scope,
                    record.provenance.strategy_stream_id,
                    record.provenance.strategy_version_id,
                    record.provenance.parent_strategy_version_id,
                    record.provenance.backtest_input_sha256,
                    record.content_sha256,
                    summary.object_id,
                    target.relative_to(self.root).as_posix(),
                    summary.created_at.isoformat(),
                ),
            )
            existing_run = connection.execute(
                """
                SELECT owner_scope, request_sha256, record_sha256,
                       backtest_object_id
                FROM runs WHERE run_id=?
                """,
                (record.run_id,),
            ).fetchone()
            if existing_run is None or tuple(existing_run) != (
                record.owner_scope,
                record.provenance.backtest_input_sha256,
                record.content_sha256,
                summary.object_id,
            ):
                raise BacktestRunIntegrityError(
                    "run index conflicts with normalized record"
                )
            connection.execute(
                """
                INSERT INTO idempotency_keys(
                    owner_scope, idempotency_key, request_sha256, run_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    record.owner_scope,
                    idempotency_key,
                    record.provenance.backtest_input_sha256,
                    record.run_id,
                ),
            )
            connection.commit()

    def _remove_transaction_files(self, manifest: Path, staged: Path) -> None:
        manifest.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        self._fsync_directory(self.transactions_dir)

    def get(
        self,
        run_id: str,
        *,
        owner_scope: str,
    ) -> PersistedBacktestRun | None:
        digest = self._parse_run_id(run_id)
        target = self._record_path(digest)
        if not target.exists() and not target.is_symlink():
            return None
        record = self._load_record(target)
        if record.run_id != run_id:
            raise BacktestRunIntegrityError("run identity does not match its path")
        if record.owner_scope != owner_scope:
            return None
        expected_object = _build_summary(record)
        stored_object = self.research_store.get(
            expected_object.object_id,
            owner_scope=owner_scope,
        )
        if stored_object is None or stored_object.payload != expected_object.payload:
            raise BacktestRunIntegrityError(
                "normalized record has no matching BacktestRun object"
            )
        return PersistedBacktestRun(
            record=record,
            object=stored_object,
            created=False,
        )

    def get_by_idempotency_key(
        self,
        *,
        owner_scope: str,
        idempotency_key: str,
    ) -> PersistedBacktestRun | None:
        row = self._idempotency_row(owner_scope, idempotency_key)
        if row is None:
            return None
        result = self.get(row["run_id"], owner_scope=owner_scope)
        if result is None:
            raise BacktestRunIntegrityError(
                "idempotency index references a missing BacktestRun"
            )
        return result

    def list_for_strategy_version(
        self,
        strategy_version_id: str,
        *,
        owner_scope: str,
    ) -> tuple[PersistedBacktestRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE owner_scope=? AND strategy_version_id=?
                ORDER BY created_at ASC, run_id ASC
                """,
                (owner_scope, strategy_version_id),
            ).fetchall()
        return self._load_indexed_runs(rows, owner_scope=owner_scope)

    def list_for_strategy_stream(
        self,
        strategy_stream_id: str,
        *,
        owner_scope: str,
    ) -> tuple[PersistedBacktestRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE owner_scope=? AND strategy_stream_id=?
                ORDER BY created_at ASC, run_id ASC
                """,
                (owner_scope, strategy_stream_id),
            ).fetchall()
        return self._load_indexed_runs(rows, owner_scope=owner_scope)

    def _load_indexed_runs(
        self,
        rows: list[sqlite3.Row],
        *,
        owner_scope: str,
    ) -> tuple[PersistedBacktestRun, ...]:
        runs: list[PersistedBacktestRun] = []
        for row in rows:
            item = self.get(row["run_id"], owner_scope=owner_scope)
            if item is None:
                raise BacktestRunIntegrityError(
                    "run index references a missing BacktestRun"
                )
            runs.append(item)
        return tuple(runs)

    def _prepare(self) -> None:
        if self.root.is_symlink():
            raise BacktestRunIntegrityError("BacktestRun store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if self.records_dir.is_symlink():
            raise BacktestRunIntegrityError("BacktestRun records must not be a symlink")
        self.records_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.records_dir, 0o700)
        self._restore_record_tombstones()
        if self.transactions_dir.is_symlink():
            raise BacktestRunIntegrityError(
                "BacktestRun transactions must not be a symlink"
            )
        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.transactions_dir, 0o700)
        if self.database_path.is_symlink():
            raise BacktestRunIntegrityError(
                "BacktestRun database must not be a symlink"
            )
        self._ensure_regular_file(self.database_path)
        if self.lock_path.exists() or self.lock_path.is_symlink():
            self._validate_regular_file(self.lock_path)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    strategy_stream_id TEXT NOT NULL,
                    strategy_version_id TEXT NOT NULL,
                    parent_strategy_version_id TEXT,
                    request_sha256 TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL UNIQUE,
                    backtest_object_id TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_version
                    ON runs(owner_scope, strategy_version_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_stream
                    ON runs(owner_scope, strategy_stream_id, created_at);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    owner_scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    PRIMARY KEY(owner_scope, idempotency_key),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
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
                raise BacktestRunIntegrityError(
                    "BacktestRun lock must be a regular file"
                )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _idempotency_row(
        self,
        owner_scope: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT request_sha256, run_id FROM idempotency_keys
                WHERE owner_scope=? AND idempotency_key=?
                """,
                (owner_scope, idempotency_key),
            ).fetchone()

    def _record_path(self, digest: str) -> Path:
        if not re.fullmatch(_SHA256_PATTERN, digest):
            raise BacktestRunIntegrityError("invalid BacktestRun digest")
        if self.records_dir.is_symlink():
            raise BacktestRunIntegrityError("BacktestRun records must not be a symlink")
        return self.records_dir / f"{digest}.json"

    def _transaction_manifest_path(self, digest: str) -> Path:
        if not re.fullmatch(_SHA256_PATTERN, digest):
            raise BacktestRunIntegrityError("invalid BacktestRun transaction digest")
        if self.transactions_dir.is_symlink():
            raise BacktestRunIntegrityError(
                "BacktestRun transactions must not be a symlink"
            )
        return self.transactions_dir / f"backtest-txn-{digest}.json"

    def _transaction_manifest_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for path in sorted(self.transactions_dir.iterdir()):
            metadata = path.lstat()
            if path.name.endswith(".record.json"):
                if not stat.S_ISREG(metadata.st_mode):
                    raise BacktestRunIntegrityError(
                        "transaction staging entry must be a regular file"
                    )
                continue
            if (
                not re.fullmatch(r"backtest-txn-[0-9a-f]{64}\.json", path.name)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise BacktestRunIntegrityError(
                    f"invalid BacktestRun transaction entry: {path}"
                )
            paths.append(path)
        return tuple(paths)

    def _clean_orphan_staging_locked(self) -> None:
        referenced = {
            self._load_transaction(path).staged_record_name
            for path in self._transaction_manifest_paths()
        }
        changed = False
        for path in self.transactions_dir.iterdir():
            if path.name.endswith(".record.json") and path.name not in referenced:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise BacktestRunIntegrityError(
                        "orphan transaction staging entry is not a regular file"
                    )
                path.unlink()
                changed = True
        if changed:
            self._fsync_directory(self.transactions_dir)

    def _record_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for path in sorted(self.records_dir.iterdir()):
            metadata = path.lstat()
            if (
                not re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise BacktestRunIntegrityError(
                    f"invalid BacktestRun record entry: {path}"
                )
            paths.append(path)
        return tuple(paths)

    def _restore_record_tombstones(self) -> None:
        for path in self.records_dir.iterdir():
            match = re.fullmatch(r"\.([0-9a-f]{64}\.json)\.delete", path.name)
            if match is None:
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                raise BacktestRunIntegrityError(
                    "BacktestRun deletion tombstone must be a regular file"
                )
            target = self.records_dir / match.group(1)
            if target.exists() or target.is_symlink():
                raise BacktestRunIntegrityError(
                    "BacktestRun record and deletion tombstone both exist"
                )
            os.replace(path, target)
            self._fsync_directory(self.records_dir)

    def _load_transaction(self, path: Path) -> _BacktestTransaction:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_048_576:
                raise BacktestRunIntegrityError(
                    "BacktestRun transaction manifest is invalid"
                )
            transaction = _BacktestTransaction.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            if isinstance(exc, BacktestRunIntegrityError):
                raise
            raise BacktestRunIntegrityError(
                f"invalid BacktestRun transaction manifest: {path}"
            ) from exc
        digest = transaction.transaction_id.rsplit(":", 1)[1]
        if path != self._transaction_manifest_path(digest):
            raise BacktestRunIntegrityError(
                "BacktestRun transaction identity does not match its path"
            )
        return transaction

    def _artifact_bytes_on_disk(self) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for path in (*self._record_paths(), *tuple(self.transactions_dir.iterdir())):
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise BacktestRunIntegrityError(
                    f"BacktestRun artifact must be a regular file: {path}"
                )
            identity = (metadata.st_dev, metadata.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += metadata.st_size
        return total

    def _validate_backup_destination(self, destination: Path) -> None:
        if destination.is_symlink():
            raise BacktestRunIntegrityError("backup destination must not be a symlink")
        target = destination.resolve(strict=False)
        for live_root in (self.root.resolve(), self.research_store.root.resolve()):
            if target == live_root or live_root in target.parents or target in live_root.parents:
                raise BacktestRunIntegrityError(
                    "backup destination must be outside all live stores"
                )
        if destination.exists() and (
            not destination.is_dir() or any(destination.iterdir())
        ):
            raise BacktestRunIntegrityError(
                "backup destination must be an empty directory"
            )

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BacktestRunIntegrityError(
                f"cannot inspect BacktestRun store file: {path}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise BacktestRunIntegrityError(
                f"BacktestRun store path must be a regular file: {path}"
            )

    @classmethod
    def _ensure_regular_file(cls, path: Path) -> None:
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise BacktestRunIntegrityError(
                f"cannot open BacktestRun store file: {path}"
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BacktestRunIntegrityError(
                    f"BacktestRun store path must be a regular file: {path}"
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
                self._validate_regular_file(path)
                os.chmod(path, 0o600)

    @staticmethod
    def _parse_run_id(run_id: str) -> str:
        if not re.fullmatch(_RUN_ID_PATTERN, run_id):
            raise BacktestRunIntegrityError("invalid BacktestRun ID")
        return run_id.rsplit(":", 1)[1]

    def _load_record(self, path: Path) -> NormalizedBacktestRecord:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise BacktestRunIntegrityError(
                        "BacktestRun record must be a regular file"
                    )
                if metadata.st_size > self.max_record_bytes:
                    raise BacktestRunIntegrityError(
                        "BacktestRun record exceeds size limit"
                    )
                chunks: list[bytes] = []
                remaining = self.max_record_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 1_048_576))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                if len(payload) > self.max_record_bytes:
                    raise BacktestRunIntegrityError(
                        "BacktestRun record exceeds size limit"
                    )
            finally:
                os.close(descriptor)
            return NormalizedBacktestRecord.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            if isinstance(exc, BacktestRunIntegrityError):
                raise
            raise BacktestRunIntegrityError(
                f"invalid normalized BacktestRun record: {path}"
            ) from exc

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
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
                pass
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

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
