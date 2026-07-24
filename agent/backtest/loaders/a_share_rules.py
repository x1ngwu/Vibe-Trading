"""Versioned A-share market-rule table used by later QE execution adapters."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AShareRuleError(ValueError):
    pass


class AShareRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    board: Literal["sh_main", "sz_main", "chinext", "star"]
    is_st: bool
    effective_from: date
    effective_to: date | None = None
    price_limit_rate: float = Field(gt=0, le=1)
    no_limit_initial_trading_days: int = Field(ge=0)
    buy_lot_shares: int = Field(gt=0)
    sell_settlement_days: Literal[1]
    source: str

    @model_validator(mode="after")
    def validate_range(self) -> "AShareRule":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


AShareBoard = Literal["sh_main", "sz_main", "chinext", "star"]


_SSE_TRADING_RULES = "SSE Trading Rules (2023 revision), sections 3.3.13-3.3.15"
_SZSE_CHINEXT_RULES = "SZSE ChiNext Trading Special Provisions, effective 2020-08-24"


A_SHARE_RULES: tuple[AShareRule, ...] = (
    AShareRule(rule_id="main-normal-v1", board="sh_main", is_st=False,
               effective_from="1996-12-16", price_limit_rate=0.10,
               no_limit_initial_trading_days=0, buy_lot_shares=100,
               sell_settlement_days=1, source=_SSE_TRADING_RULES),
    AShareRule(rule_id="sz-main-normal-v1", board="sz_main", is_st=False,
               effective_from="1996-12-16", price_limit_rate=0.10,
               no_limit_initial_trading_days=0, buy_lot_shares=100,
               sell_settlement_days=1, source=_SSE_TRADING_RULES),
    AShareRule(rule_id="main-st-v1", board="sh_main", is_st=True,
               effective_from="1998-04-22", price_limit_rate=0.05,
               no_limit_initial_trading_days=0, buy_lot_shares=100,
               sell_settlement_days=1, source=_SSE_TRADING_RULES),
    AShareRule(rule_id="sz-main-st-v1", board="sz_main", is_st=True,
               effective_from="1998-04-22", price_limit_rate=0.05,
               no_limit_initial_trading_days=0, buy_lot_shares=100,
               sell_settlement_days=1, source=_SSE_TRADING_RULES),
    AShareRule(rule_id="chinext-normal-v1", board="chinext", is_st=False,
               effective_from="2009-10-30", effective_to="2020-08-23",
               price_limit_rate=0.10, no_limit_initial_trading_days=0,
               buy_lot_shares=100, sell_settlement_days=1,
               source=_SZSE_CHINEXT_RULES),
    AShareRule(rule_id="chinext-normal-v2", board="chinext", is_st=False,
               effective_from="2020-08-24", price_limit_rate=0.20,
               no_limit_initial_trading_days=5, buy_lot_shares=100,
               sell_settlement_days=1, source=_SZSE_CHINEXT_RULES),
    AShareRule(rule_id="star-normal-v1", board="star", is_st=False,
               effective_from="2019-07-22", price_limit_rate=0.20,
               no_limit_initial_trading_days=5, buy_lot_shares=100,
               sell_settlement_days=1, source=_SSE_TRADING_RULES),
)


def resolve_a_share_rule(*, board: AShareBoard, is_st: bool, trade_date: date) -> AShareRule:
    """Resolve exactly one effective rule; ambiguity and uncovered dates fail closed."""

    matches = tuple(
        rule for rule in A_SHARE_RULES
        if rule.board == board
        and rule.is_st == is_st
        and rule.effective_from <= trade_date
        and (rule.effective_to is None or trade_date <= rule.effective_to)
    )
    if len(matches) != 1:
        raise AShareRuleError(
            f"expected one A-share rule for board={board}, is_st={is_st}, "
            f"trade_date={trade_date.isoformat()}; found={len(matches)}"
        )
    return matches[0]
