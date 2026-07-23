"""Hand-calculated QE1 ledger contract and accounting invariant checks."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import canonical_sha256

GOLDEN_LEDGER_SCHEMA = "vibe.golden-ledger.v1"


class GoldenLedgerError(ValueError):
    """Raised when a frozen ledger violates an exact accounting invariant."""


class _StrictLedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LedgerRules(_StrictLedgerModel):
    currency: Literal["CNY"]
    money_unit: Literal["fen"]
    rounding: Literal["round_half_up_per_charge"]
    settlement: Literal["cn_equity_t_plus_one"]
    lot_size: int = Field(gt=0)
    commission_bps: int = Field(ge=0)
    minimum_commission_fen: int = Field(ge=0)
    sell_tax_bps: int = Field(ge=0)
    transfer_fee_bps: int = Field(ge=0)
    rule_version: str


class LedgerStep(_StrictLedgerModel):
    sequence: int = Field(ge=0)
    trade_date: date
    event: Literal[
        "opening",
        "buy",
        "rejected_t1_sell",
        "share_split",
        "dividend_ex",
        "dividend_pay",
        "sell",
    ]
    symbol: str | None
    price_fen: int | None = Field(default=None, gt=0)
    position_delta: dict[str, int]
    cash_delta_fen: int
    fee_fen: int = Field(ge=0)
    dividend_receivable_delta_fen: int
    positions: dict[str, int]
    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)
    mark_prices_fen: dict[str, int]
    market_value_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)
    rejection_reason: Literal["T1_LOCKED"] | None = None
    note: str


class GoldenLedger(_StrictLedgerModel):
    schema_version: Literal["vibe.golden-ledger.v1"] = GOLDEN_LEDGER_SCHEMA
    ledger_id: Literal["qe1-cn-account-v1"]
    market_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules: LedgerRules
    steps: tuple[LedgerStep, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_accounting(self) -> "GoldenLedger":
        previous: LedgerStep | None = None
        for expected_sequence, step in enumerate(self.steps):
            if step.sequence != expected_sequence:
                raise ValueError("ledger sequence must be contiguous and start at zero")
            if previous is not None and step.trade_date < previous.trade_date:
                raise ValueError("ledger dates must be non-decreasing")

            prior_cash = 0 if previous is None else previous.cash_fen
            prior_receivable = 0 if previous is None else previous.dividend_receivable_fen
            prior_positions = {} if previous is None else previous.positions
            expected_positions = dict(prior_positions)
            for symbol, quantity_delta in step.position_delta.items():
                expected_positions[symbol] = expected_positions.get(symbol, 0) + quantity_delta
                if expected_positions[symbol] == 0:
                    del expected_positions[symbol]
                elif expected_positions[symbol] < 0:
                    raise ValueError("ledger position cannot become negative")

            if step.cash_fen != prior_cash + step.cash_delta_fen:
                raise ValueError(f"cash invariant failed at sequence {step.sequence}")
            if step.dividend_receivable_fen != prior_receivable + step.dividend_receivable_delta_fen:
                raise ValueError(f"dividend invariant failed at sequence {step.sequence}")
            if step.positions != expected_positions:
                raise ValueError(f"position invariant failed at sequence {step.sequence}")
            if set(step.mark_prices_fen) != set(step.positions):
                raise ValueError(f"every open position needs exactly one mark at sequence {step.sequence}")
            expected_market_value = sum(
                quantity * step.mark_prices_fen[symbol]
                for symbol, quantity in step.positions.items()
            )
            if step.market_value_fen != expected_market_value:
                raise ValueError(f"market value invariant failed at sequence {step.sequence}")
            if step.equity_fen != step.cash_fen + step.dividend_receivable_fen + step.market_value_fen:
                raise ValueError(f"equity invariant failed at sequence {step.sequence}")
            self._validate_event_cash_flow(step)
            previous = step
        return self

    @staticmethod
    def _validate_event_cash_flow(step: LedgerStep) -> None:
        if step.event == "opening":
            if step.sequence != 0 or step.position_delta or step.fee_fen:
                raise ValueError("opening step must initialize only cash")
            return
        if step.event in {"buy", "sell"}:
            if step.symbol is None or step.price_fen is None:
                raise ValueError("trade step requires symbol and price")
            quantity_delta = step.position_delta.get(step.symbol)
            if quantity_delta is None or len(step.position_delta) != 1:
                raise ValueError("trade step requires exactly one matching position delta")
            if step.event == "buy":
                expected_cash_delta = -(quantity_delta * step.price_fen + step.fee_fen)
                if quantity_delta <= 0:
                    raise ValueError("buy quantity must be positive")
            else:
                expected_cash_delta = -quantity_delta * step.price_fen - step.fee_fen
                if quantity_delta >= 0:
                    raise ValueError("sell quantity must be negative")
            if step.cash_delta_fen != expected_cash_delta:
                raise ValueError(f"trade cash-flow invariant failed at sequence {step.sequence}")
            return
        if step.event == "rejected_t1_sell":
            if (
                step.position_delta
                or step.cash_delta_fen
                or step.fee_fen
                or step.dividend_receivable_delta_fen
                or step.rejection_reason != "T1_LOCKED"
            ):
                raise ValueError("rejected T+1 sale must have no accounting effect")
            return
        if step.fee_fen or step.event in {"share_split", "dividend_ex"} and step.cash_delta_fen:
            raise ValueError("non-trade event has an unexpected cash fee or flow")
        if step.event == "dividend_pay" and step.cash_delta_fen != -step.dividend_receivable_delta_fen:
            raise ValueError("dividend payment must move receivable into cash exactly")

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self)


def load_golden_ledger(path: Path) -> GoldenLedger:
    """Load a frozen ledger from a local regular file without network access."""

    ledger_path = Path(path)
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise GoldenLedgerError("golden ledger must be a regular, non-symlink file")
    return GoldenLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
