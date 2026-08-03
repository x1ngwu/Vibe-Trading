"""Deterministic A-share execution and accounting oracle for QE5.

The module deliberately uses integer shares and integer fen.  Fee rates are
stored as tenths of a basis point, so the 0.1 bps transfer fee is represented
without binary floating-point arithmetic.  It is engine-neutral: QE5-2 can
compare QUANTAXIS output with this ledger instead of trusting a legacy
backtester's internal cash calculations.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import canonical_sha256

CN_EQUITY_LEDGER_SCHEMA = "vibe.cn-equity-ledger.v1"
_SYMBOL_PATTERN = r"^[0-9]{6}\.(?:SH|SZ|BJ)$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

OrderSide = Literal["buy", "sell"]
MarketState = Literal[
    "open",
    "suspended",
    "locked_limit_up",
    "locked_limit_down",
    "delisted",
]
LedgerEvent = Literal[
    "opening",
    "buy",
    "sell",
    "rejected_order",
    "share_split",
    "dividend_ex",
    "dividend_pay",
    "mark",
]
OrderOutcome = Literal[
    "not_applicable",
    "filled",
    "partially_filled",
    "rejected",
    "applied",
]
ExecutionReason = Literal[
    "BOARD_LOT_ROUNDED",
    "CAPACITY_LIMITED",
    "DELISTED",
    "INSUFFICIENT_CASH",
    "INSUFFICIENT_POSITION",
    "INSUFFICIENT_SELLABLE",
    "INVALID_BOARD_LOT",
    "LIMIT_DOWN_LOCKED",
    "LIMIT_UP_LOCKED",
    "NO_LIQUIDITY",
    "NO_POSITION",
    "SUSPENDED",
    "T1_LOCKED",
]


class CnEquityAccountingError(ValueError):
    """Raised when deterministic accounting input is invalid or ambiguous."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CnEquityFeeSchedule(_StrictModel):
    """Versioned A-share fee inputs expressed in exact 0.1 bps units."""

    commission_tenths_bps: int = Field(ge=0)
    minimum_commission_fen: int = Field(ge=0)
    sell_tax_tenths_bps: int = Field(ge=0)
    transfer_fee_tenths_bps: int = Field(ge=0)
    rule_version: str = Field(pattern=_ID_PATTERN)


class CnEquityOrder(_StrictModel):
    """One normalized order plus immutable market execution facts."""

    order_id: str = Field(pattern=_ID_PATTERN)
    trade_date: date
    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    side: OrderSide
    requested_shares: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    market_state: MarketState = "open"
    maximum_fill_shares: int | None = Field(default=None, ge=0)


class CnEquityFeeBreakdown(_StrictModel):
    """Per-fill charges rounded independently to fen."""

    commission_fen: int = Field(ge=0)
    sell_tax_fen: int = Field(ge=0)
    transfer_fee_fen: int = Field(ge=0)
    total_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "CnEquityFeeBreakdown":
        if self.total_fen != (
            self.commission_fen + self.sell_tax_fen + self.transfer_fee_fen
        ):
            raise ValueError("fee components do not sum to total_fen")
        return self


_ZERO_FEES = CnEquityFeeBreakdown(
    commission_fen=0,
    sell_tax_fen=0,
    transfer_fee_fen=0,
    total_fen=0,
)


class CnEquityLedgerEntry(_StrictModel):
    """One fully materialized state transition."""

    sequence: int = Field(ge=0)
    trade_date: date
    event: LedgerEvent
    outcome: OrderOutcome
    reason: ExecutionReason | None = None
    order_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    symbol: str | None = Field(default=None, pattern=_SYMBOL_PATTERN)
    side: OrderSide | None = None
    requested_shares: int = Field(default=0, ge=0)
    filled_shares: int = Field(default=0, ge=0)
    price_fen: int | None = Field(default=None, gt=0)
    position_delta: dict[str, int] = Field(default_factory=dict)
    cash_delta_fen: int = 0
    dividend_receivable_delta_fen: int = 0
    fees: CnEquityFeeBreakdown = _ZERO_FEES
    positions: dict[str, int] = Field(default_factory=dict)
    sellable_positions: dict[str, int] = Field(default_factory=dict)
    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)
    mark_prices_fen: dict[str, int] = Field(default_factory=dict)
    market_value_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_entry_shape(self) -> "CnEquityLedgerEntry":
        if set(self.positions) != set(self.mark_prices_fen):
            raise ValueError("every open position requires exactly one mark")
        if set(self.sellable_positions) - set(self.positions):
            raise ValueError("sellable positions must be a subset of positions")
        for symbol, quantity in self.positions.items():
            if quantity <= 0:
                raise ValueError("positions must be positive")
            sellable = self.sellable_positions.get(symbol, 0)
            if not 0 <= sellable <= quantity:
                raise ValueError("sellable quantity is outside the held position")
        expected_market_value = sum(
            quantity * self.mark_prices_fen[symbol]
            for symbol, quantity in self.positions.items()
        )
        if self.market_value_fen != expected_market_value:
            raise ValueError("market value does not match positions and marks")
        if self.equity_fen != (
            self.cash_fen + self.dividend_receivable_fen + self.market_value_fen
        ):
            raise ValueError("equity does not satisfy the accounting identity")
        if self.outcome == "rejected" and (
            self.filled_shares
            or self.position_delta
            or self.cash_delta_fen
            or self.dividend_receivable_delta_fen
            or self.fees.total_fen
        ):
            raise ValueError("rejected orders cannot have an accounting effect")
        if self.outcome in {"filled", "partially_filled"} and not self.filled_shares:
            raise ValueError("filled orders require a positive filled_shares")
        if self.outcome == "filled" and self.reason is not None:
            raise ValueError("fully filled orders cannot carry a partial-fill reason")
        if self.outcome == "partially_filled" and self.reason not in {
            "BOARD_LOT_ROUNDED",
            "CAPACITY_LIMITED",
        }:
            raise ValueError("partial fills require a normalization or capacity reason")
        if self.outcome == "rejected" and self.reason in {
            "BOARD_LOT_ROUNDED",
            "CAPACITY_LIMITED",
        }:
            raise ValueError("rejections cannot use a partial-fill reason")
        if self.event == "opening":
            if (
                self.sequence != 0
                or self.outcome != "not_applicable"
                or self.order_id is not None
                or self.symbol is not None
                or self.position_delta
                or self.fees.total_fen
                or self.dividend_receivable_delta_fen
            ):
                raise ValueError("opening entry has an invalid event shape")
        elif self.event in {"buy", "sell"}:
            if (
                self.order_id is None
                or self.symbol is None
                or self.side != self.event
                or self.price_fen is None
                or self.outcome not in {"filled", "partially_filled"}
            ):
                raise ValueError("trade entry has an invalid event shape")
            expected_delta = self.filled_shares if self.event == "buy" else -self.filled_shares
            if self.position_delta != {self.symbol: expected_delta}:
                raise ValueError("trade position delta does not match its fill")
            notional_fen = self.filled_shares * self.price_fen
            expected_cash_delta = (
                -(notional_fen + self.fees.total_fen)
                if self.event == "buy"
                else notional_fen - self.fees.total_fen
            )
            if self.cash_delta_fen != expected_cash_delta:
                raise ValueError("trade cash flow does not match its fill and fees")
            if self.dividend_receivable_delta_fen:
                raise ValueError("trade entry cannot change dividend receivables")
        elif self.event == "rejected_order":
            if (
                self.order_id is None
                or self.symbol is None
                or self.side is None
                or self.outcome != "rejected"
                or self.reason is None
            ):
                raise ValueError("rejected order has an invalid event shape")
        else:
            if (
                self.order_id is not None
                or self.side is not None
                or self.requested_shares
                or self.filled_shares
                or self.fees.total_fen
            ):
                raise ValueError("non-order event contains order accounting")
            if self.event == "share_split" and (
                self.outcome != "applied"
                or self.symbol is None
                or not self.position_delta
                or self.cash_delta_fen
                or self.dividend_receivable_delta_fen
            ):
                raise ValueError("share split has an invalid event shape")
            if self.event == "dividend_ex" and (
                self.outcome != "applied"
                or self.symbol is None
                or self.position_delta
                or self.cash_delta_fen
                or self.dividend_receivable_delta_fen <= 0
            ):
                raise ValueError("dividend accrual has an invalid event shape")
            if self.event == "dividend_pay" and (
                self.outcome != "applied"
                or self.symbol is None
                or self.position_delta
                or self.cash_delta_fen <= 0
                or self.cash_delta_fen != -self.dividend_receivable_delta_fen
            ):
                raise ValueError("dividend payment has an invalid event shape")
            if self.event == "mark" and (
                self.outcome != "applied"
                or self.symbol is not None
                or self.position_delta
                or self.cash_delta_fen
                or self.dividend_receivable_delta_fen
            ):
                raise ValueError("mark entry has an invalid event shape")
        return self


class CnEquityLedger(_StrictModel):
    """Canonical output of the deterministic A-share account oracle."""

    schema_version: Literal["vibe.cn-equity-ledger.v1"] = CN_EQUITY_LEDGER_SCHEMA
    ledger_id: str = Field(pattern=_ID_PATTERN)
    data_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules: CnEquityFeeSchedule
    board_lot: int = Field(gt=0)
    entries: tuple[CnEquityLedgerEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chain(self) -> "CnEquityLedger":
        previous: CnEquityLedgerEntry | None = None
        for expected_sequence, entry in enumerate(self.entries):
            if entry.sequence != expected_sequence:
                raise ValueError("ledger sequence must be contiguous and start at zero")
            if previous is None:
                if entry.event != "opening":
                    raise ValueError("ledger must begin with an opening entry")
                prior_cash = 0
                prior_receivable = 0
                prior_positions: dict[str, int] = {}
            else:
                if entry.trade_date < previous.trade_date:
                    raise ValueError("ledger dates must be non-decreasing")
                prior_cash = previous.cash_fen
                prior_receivable = previous.dividend_receivable_fen
                prior_positions = previous.positions
            expected_positions = dict(prior_positions)
            for symbol, delta in entry.position_delta.items():
                expected_positions[symbol] = expected_positions.get(symbol, 0) + delta
                if expected_positions[symbol] == 0:
                    del expected_positions[symbol]
                elif expected_positions[symbol] < 0:
                    raise ValueError("ledger position cannot become negative")
            if entry.positions != expected_positions:
                raise ValueError(f"position invariant failed at sequence {entry.sequence}")
            if entry.cash_fen != prior_cash + entry.cash_delta_fen:
                raise ValueError(f"cash invariant failed at sequence {entry.sequence}")
            if entry.dividend_receivable_fen != (
                prior_receivable + entry.dividend_receivable_delta_fen
            ):
                raise ValueError(
                    f"dividend invariant failed at sequence {entry.sequence}"
                )
            previous = entry
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self)


def _round_half_up_rate(notional_fen: int, tenths_bps: int) -> int:
    """Round ``notional × tenths-of-bps`` to the nearest fen, ties upward."""

    numerator = notional_fen * tenths_bps
    quotient, remainder = divmod(numerator, 100_000)
    if remainder * 2 >= 100_000:
        quotient += 1
    return quotient


def calculate_cn_equity_fees(
    *,
    shares: int,
    price_fen: int,
    side: OrderSide,
    rules: CnEquityFeeSchedule,
) -> CnEquityFeeBreakdown:
    """Return independently rounded A-share commission, tax, and transfer fee."""

    if shares <= 0 or price_fen <= 0:
        raise CnEquityAccountingError("fee calculation requires positive shares and price")
    notional_fen = shares * price_fen
    commission_fen = max(
        _round_half_up_rate(notional_fen, rules.commission_tenths_bps),
        rules.minimum_commission_fen,
    )
    transfer_fee_fen = _round_half_up_rate(
        notional_fen,
        rules.transfer_fee_tenths_bps,
    )
    sell_tax_fen = (
        _round_half_up_rate(notional_fen, rules.sell_tax_tenths_bps)
        if side == "sell"
        else 0
    )
    return CnEquityFeeBreakdown(
        commission_fen=commission_fen,
        sell_tax_fen=sell_tax_fen,
        transfer_fee_fen=transfer_fee_fen,
        total_fen=commission_fen + sell_tax_fen + transfer_fee_fen,
    )


class CnEquityAccount:
    """Mutable builder that emits an immutable, validated canonical ledger."""

    def __init__(
        self,
        *,
        ledger_id: str,
        data_snapshot_sha256: str,
        opening_date: date,
        initial_cash_fen: int,
        rules: CnEquityFeeSchedule,
        board_lot: int = 100,
    ) -> None:
        if initial_cash_fen < 0:
            raise CnEquityAccountingError("initial cash cannot be negative")
        if board_lot <= 0:
            raise CnEquityAccountingError("board lot must be positive")
        self.ledger_id = ledger_id
        self.data_snapshot_sha256 = data_snapshot_sha256
        self.rules = rules
        self.board_lot = board_lot
        self._cash_fen = initial_cash_fen
        self._receivables: dict[str, int] = {}
        self._lots: dict[str, list[list[date | int]]] = defaultdict(list)
        self._marks: dict[str, int] = {}
        self._entries: list[CnEquityLedgerEntry] = []
        self._append(
            trade_date=opening_date,
            event="opening",
            outcome="not_applicable",
            cash_delta_fen=initial_cash_fen,
        )

    @property
    def cash_fen(self) -> int:
        return self._cash_fen

    @property
    def positions(self) -> dict[str, int]:
        return {
            symbol: sum(int(lot[1]) for lot in lots)
            for symbol, lots in self._lots.items()
            if lots
        }

    def sellable_positions(self, trade_date: date) -> dict[str, int]:
        return {
            symbol: sum(
                int(quantity)
                for acquired_date, quantity in lots
                if acquired_date < trade_date
            )
            for symbol, lots in self._lots.items()
            if lots
        }

    def _require_chronological(self, trade_date: date) -> None:
        if self._entries and trade_date < self._entries[-1].trade_date:
            raise CnEquityAccountingError("account events must be chronological")

    def _replace_marks(self, marks: Mapping[str, int] | None) -> None:
        if marks is not None:
            positions = self.positions
            if set(marks) != set(positions):
                raise CnEquityAccountingError(
                    "marks must cover every and only the current positions"
                )
            if any(
                not isinstance(price, int) or isinstance(price, bool) or price <= 0
                for price in marks.values()
            ):
                raise CnEquityAccountingError("mark prices must be positive integer fen")
            self._marks = dict(marks)
        if set(self._marks) != set(self.positions):
            raise CnEquityAccountingError(
                "every account transition requires marks for all open positions"
            )

    def _append(
        self,
        *,
        trade_date: date,
        event: LedgerEvent,
        outcome: OrderOutcome,
        reason: ExecutionReason | None = None,
        order: CnEquityOrder | None = None,
        filled_shares: int = 0,
        position_delta: Mapping[str, int] | None = None,
        cash_delta_fen: int = 0,
        dividend_receivable_delta_fen: int = 0,
        fees: CnEquityFeeBreakdown = _ZERO_FEES,
        marks: Mapping[str, int] | None = None,
        symbol: str | None = None,
        price_fen: int | None = None,
    ) -> CnEquityLedgerEntry:
        self._require_chronological(trade_date)
        self._replace_marks(marks)
        positions = self.positions
        sellable = self.sellable_positions(trade_date)
        market_value = sum(
            quantity * self._marks[held_symbol]
            for held_symbol, quantity in positions.items()
        )
        entry = CnEquityLedgerEntry(
            sequence=len(self._entries),
            trade_date=trade_date,
            event=event,
            outcome=outcome,
            reason=reason,
            order_id=order.order_id if order else None,
            symbol=order.symbol if order else symbol,
            side=order.side if order else None,
            requested_shares=order.requested_shares if order else 0,
            filled_shares=filled_shares,
            price_fen=order.price_fen if order else price_fen,
            position_delta=dict(position_delta or {}),
            cash_delta_fen=cash_delta_fen,
            dividend_receivable_delta_fen=dividend_receivable_delta_fen,
            fees=fees,
            positions=positions,
            sellable_positions=sellable,
            cash_fen=self._cash_fen,
            dividend_receivable_fen=sum(self._receivables.values()),
            mark_prices_fen=dict(self._marks),
            market_value_fen=market_value,
            equity_fen=self._cash_fen
            + sum(self._receivables.values())
            + market_value,
        )
        self._entries.append(entry)
        return entry

    def _reject(
        self,
        order: CnEquityOrder,
        reason: ExecutionReason,
    ) -> CnEquityLedgerEntry:
        if order.symbol in self.positions:
            self._marks[order.symbol] = order.price_fen
        return self._append(
            trade_date=order.trade_date,
            event="rejected_order",
            outcome="rejected",
            reason=reason,
            order=order,
        )

    def submit_order(self, order: CnEquityOrder) -> CnEquityLedgerEntry:
        """Apply one order or emit a no-effect, typed rejection."""

        self._require_chronological(order.trade_date)
        if order.market_state == "suspended":
            return self._reject(order, "SUSPENDED")
        if order.market_state == "delisted":
            return self._reject(order, "DELISTED")
        if order.market_state == "locked_limit_up" and order.side == "buy":
            return self._reject(order, "LIMIT_UP_LOCKED")
        if order.market_state == "locked_limit_down" and order.side == "sell":
            return self._reject(order, "LIMIT_DOWN_LOCKED")

        if order.side == "buy":
            return self._buy(order)
        return self._sell(order)

    def _buy(self, order: CnEquityOrder) -> CnEquityLedgerEntry:
        normalized_request = order.requested_shares // self.board_lot * self.board_lot
        if normalized_request == 0:
            return self._reject(order, "INVALID_BOARD_LOT")
        capacity = (
            normalized_request
            if order.maximum_fill_shares is None
            else order.maximum_fill_shares // self.board_lot * self.board_lot
        )
        if capacity == 0:
            return self._reject(order, "NO_LIQUIDITY")
        filled_shares = min(normalized_request, capacity)
        fees = calculate_cn_equity_fees(
            shares=filled_shares,
            price_fen=order.price_fen,
            side="buy",
            rules=self.rules,
        )
        cash_delta = -(filled_shares * order.price_fen + fees.total_fen)
        if self._cash_fen + cash_delta < 0:
            return self._reject(order, "INSUFFICIENT_CASH")

        self._cash_fen += cash_delta
        self._lots[order.symbol].append([order.trade_date, filled_shares])
        self._marks[order.symbol] = order.price_fen
        if filled_shares < normalized_request:
            reason: ExecutionReason | None = "CAPACITY_LIMITED"
        elif normalized_request < order.requested_shares:
            reason = "BOARD_LOT_ROUNDED"
        else:
            reason = None
        return self._append(
            trade_date=order.trade_date,
            event="buy",
            outcome="partially_filled" if reason else "filled",
            reason=reason,
            order=order,
            filled_shares=filled_shares,
            position_delta={order.symbol: filled_shares},
            cash_delta_fen=cash_delta,
            fees=fees,
        )

    def _sell(self, order: CnEquityOrder) -> CnEquityLedgerEntry:
        held = self.positions.get(order.symbol, 0)
        if held == 0:
            return self._reject(order, "NO_POSITION")
        if order.requested_shares > held:
            return self._reject(order, "INSUFFICIENT_POSITION")
        sellable = self.sellable_positions(order.trade_date).get(order.symbol, 0)
        if sellable == 0:
            return self._reject(order, "T1_LOCKED")
        if order.requested_shares > sellable:
            return self._reject(order, "INSUFFICIENT_SELLABLE")
        capacity = (
            order.requested_shares
            if order.maximum_fill_shares is None
            else order.maximum_fill_shares
        )
        if capacity == 0:
            return self._reject(order, "NO_LIQUIDITY")
        filled_shares = min(order.requested_shares, capacity)
        fees = calculate_cn_equity_fees(
            shares=filled_shares,
            price_fen=order.price_fen,
            side="sell",
            rules=self.rules,
        )
        cash_delta = filled_shares * order.price_fen - fees.total_fen
        self._consume_sellable_lots(
            symbol=order.symbol,
            shares=filled_shares,
            trade_date=order.trade_date,
        )
        self._cash_fen += cash_delta
        if order.symbol in self.positions:
            self._marks[order.symbol] = order.price_fen
        else:
            self._marks.pop(order.symbol, None)
        reason: ExecutionReason | None = (
            "CAPACITY_LIMITED" if filled_shares < order.requested_shares else None
        )
        return self._append(
            trade_date=order.trade_date,
            event="sell",
            outcome="partially_filled" if reason else "filled",
            reason=reason,
            order=order,
            filled_shares=filled_shares,
            position_delta={order.symbol: -filled_shares},
            cash_delta_fen=cash_delta,
            fees=fees,
        )

    def _consume_sellable_lots(
        self,
        *,
        symbol: str,
        shares: int,
        trade_date: date,
    ) -> None:
        remaining = shares
        next_lots: list[list[date | int]] = []
        for acquired_date, raw_quantity in self._lots[symbol]:
            quantity = int(raw_quantity)
            if acquired_date < trade_date and remaining:
                consumed = min(quantity, remaining)
                quantity -= consumed
                remaining -= consumed
            if quantity:
                next_lots.append([acquired_date, quantity])
        if remaining:
            raise CnEquityAccountingError("sellable lot accounting underflow")
        if next_lots:
            self._lots[symbol] = next_lots
        else:
            del self._lots[symbol]

    def apply_share_split(
        self,
        *,
        trade_date: date,
        symbol: str,
        multiplier_numerator: int,
        multiplier_denominator: int,
        mark_prices_fen: Mapping[str, int],
    ) -> CnEquityLedgerEntry:
        """Multiply every acquisition lot exactly; fractional shares fail closed."""

        self._require_chronological(trade_date)
        if multiplier_numerator <= 0 or multiplier_denominator <= 0:
            raise CnEquityAccountingError("share multiplier must be positive")
        before = self.positions.get(symbol, 0)
        if before == 0:
            raise CnEquityAccountingError("share split requires an existing position")
        new_lots: list[list[date | int]] = []
        for acquired_date, raw_quantity in self._lots[symbol]:
            numerator = int(raw_quantity) * multiplier_numerator
            quantity, remainder = divmod(numerator, multiplier_denominator)
            if remainder:
                raise CnEquityAccountingError(
                    "share split would create a fractional share"
                )
            new_lots.append([acquired_date, quantity])
        self._replace_marks(mark_prices_fen)
        price_fen = mark_prices_fen[symbol]
        self._lots[symbol] = new_lots
        after = self.positions[symbol]
        return self._append(
            trade_date=trade_date,
            event="share_split",
            outcome="applied",
            symbol=symbol,
            position_delta={symbol: after - before},
            marks=None,
            price_fen=price_fen,
        )

    def accrue_dividend(
        self,
        *,
        trade_date: date,
        symbol: str,
        entitled_shares: int,
        cash_per_share_fen: int | None = None,
        cash_per_share_numerator_fen: int | None = None,
        cash_per_share_denominator: int | None = None,
        cash_rounding: str | None = None,
        mark_prices_fen: Mapping[str, int],
    ) -> CnEquityLedgerEntry:
        """Accrue an exact rational cash dividend without crediting cash early."""

        self._require_chronological(trade_date)
        held = self.positions.get(symbol, 0)
        if entitled_shares <= 0 or entitled_shares > held:
            raise CnEquityAccountingError(
                "dividend entitlement exceeds the held record-date position"
            )
        if cash_per_share_fen is not None:
            if (
                cash_per_share_fen <= 0
                or cash_per_share_numerator_fen is not None
                or cash_per_share_denominator is not None
                or cash_rounding is not None
            ):
                raise CnEquityAccountingError(
                    "cash dividend must use one positive amount representation"
                )
            numerator = cash_per_share_fen
            denominator = 1
        else:
            if (
                cash_per_share_numerator_fen is None
                or cash_per_share_numerator_fen <= 0
                or cash_per_share_denominator is None
                or cash_per_share_denominator <= 0
            ):
                raise CnEquityAccountingError(
                    "cash dividend rational amount must be positive"
                )
            numerator = cash_per_share_numerator_fen
            denominator = cash_per_share_denominator
            if cash_rounding not in {
                None,
                "reject_fractional_fen",
                "half_up_total_fen",
            }:
                raise CnEquityAccountingError(
                    "cash dividend rounding policy is unsupported"
                )
        amount, remainder = divmod(entitled_shares * numerator, denominator)
        if remainder and cash_rounding == "half_up_total_fen":
            amount += int(remainder * 2 >= denominator)
        elif remainder:
            raise CnEquityAccountingError(
                "cash dividend entitlement is not representable in integer fen"
            )
        self._replace_marks(mark_prices_fen)
        price_fen = mark_prices_fen[symbol]
        self._receivables[symbol] = self._receivables.get(symbol, 0) + amount
        return self._append(
            trade_date=trade_date,
            event="dividend_ex",
            outcome="applied",
            symbol=symbol,
            dividend_receivable_delta_fen=amount,
            marks=None,
            price_fen=price_fen,
        )

    def pay_dividend(
        self,
        *,
        trade_date: date,
        symbol: str,
        mark_prices_fen: Mapping[str, int],
    ) -> CnEquityLedgerEntry:
        """Move one symbol's exact receivable into cash."""

        self._require_chronological(trade_date)
        amount = self._receivables.get(symbol, 0)
        if amount <= 0:
            raise CnEquityAccountingError("dividend payment has no receivable")
        # Dividend entitlement is fixed on the record date.  The investor may
        # legitimately sell the full position before the payment date, so the
        # paid symbol does not necessarily have a current valuation mark.
        # Validate and install the post-event marks before mutating cash or the
        # receivable to keep failures atomic.
        self._replace_marks(mark_prices_fen)
        price_fen = mark_prices_fen.get(symbol)
        del self._receivables[symbol]
        self._cash_fen += amount
        return self._append(
            trade_date=trade_date,
            event="dividend_pay",
            outcome="applied",
            symbol=symbol,
            cash_delta_fen=amount,
            dividend_receivable_delta_fen=-amount,
            marks=None,
            price_fen=price_fen,
        )

    def mark(
        self,
        *,
        trade_date: date,
        mark_prices_fen: Mapping[str, int],
    ) -> CnEquityLedgerEntry:
        """Materialize a no-cash daily valuation transition."""

        return self._append(
            trade_date=trade_date,
            event="mark",
            outcome="applied",
            marks=mark_prices_fen,
        )

    def ledger(self) -> CnEquityLedger:
        """Freeze and validate the current account history."""

        return CnEquityLedger(
            ledger_id=self.ledger_id,
            data_snapshot_sha256=self.data_snapshot_sha256,
            rules=self.rules,
            board_lot=self.board_lot,
            entries=tuple(self._entries),
        )
