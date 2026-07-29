"""Deterministic QE4 allowlist for the first household strategy DSL.

The registry only describes fields that already have a bounded, auditable
calculation path.  It is deliberately independent from LLM output: natural
language can select a capability, but cannot add one.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.research.contracts import SignalRule

STRATEGY_DSL_VERSION = "vibe.strategy-spec.v1"

StrategyFieldKind = Literal["market", "factor"]
StrategyFieldSource = Literal["snapshot", "qe3_factor", "quantaxis"]
SignalOperator = Literal[
    "gt",
    "gte",
    "lt",
    "lte",
    "crosses_above",
    "crosses_below",
]

_THRESHOLD_OPERATORS: tuple[SignalOperator, ...] = ("gt", "gte", "lt", "lte")
_CROSSING_OPERATORS: tuple[SignalOperator, ...] = (
    "crosses_above",
    "crosses_below",
)
_ALL_OPERATORS = _THRESHOLD_OPERATORS + _CROSSING_OPERATORS
_DYNAMIC_FACTOR_RE = re.compile(r"^(ma|ema)_([0-9]{1,3})$")


class StrategyFieldCapability(BaseModel):
    """One immutable strategy field exposed by the first DSL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: StrategyFieldKind
    source: StrategyFieldSource
    allowed_operators: tuple[SignalOperator, ...]
    lookback_days: int = Field(ge=1, le=512)
    available_at: Literal["close"] = "close"
    numeric: Literal[True] = True


_EXACT_CAPABILITIES: Mapping[str, StrategyFieldCapability] = MappingProxyType(
    {
        "close": StrategyFieldCapability(
            field_id="close",
            kind="market",
            source="snapshot",
            allowed_operators=_ALL_OPERATORS,
            lookback_days=1,
        ),
        "drawdown_20d": StrategyFieldCapability(
            field_id="drawdown_20d",
            kind="factor",
            source="qe3_factor",
            allowed_operators=_THRESHOLD_OPERATORS,
            lookback_days=20,
        ),
        "momentum_20d": StrategyFieldCapability(
            field_id="momentum_20d",
            kind="factor",
            source="qe3_factor",
            allowed_operators=_THRESHOLD_OPERATORS,
            lookback_days=20,
        ),
        "turnover_change_20d": StrategyFieldCapability(
            field_id="turnover_change_20d",
            kind="factor",
            source="qe3_factor",
            allowed_operators=_THRESHOLD_OPERATORS,
            lookback_days=20,
        ),
        "volatility_20d": StrategyFieldCapability(
            field_id="volatility_20d",
            kind="factor",
            source="qe3_factor",
            allowed_operators=_THRESHOLD_OPERATORS,
            lookback_days=20,
        ),
    }
)


def resolve_strategy_field(field_id: str) -> StrategyFieldCapability | None:
    """Resolve one exact or bounded parametric strategy field."""

    exact = _EXACT_CAPABILITIES.get(field_id)
    if exact is not None:
        return exact
    match = _DYNAMIC_FACTOR_RE.fullmatch(field_id)
    if match is None:
        return None
    name, raw_window = match.groups()
    window = int(raw_window)
    if not 2 <= window <= 512:
        return None
    return StrategyFieldCapability(
        field_id=field_id,
        kind="factor",
        source="quantaxis",
        allowed_operators=_ALL_OPERATORS,
        lookback_days=window,
    )


def list_strategy_field_capabilities() -> tuple[StrategyFieldCapability, ...]:
    """Return the stable exact registry used for UI/help projections."""

    return tuple(_EXACT_CAPABILITIES[key] for key in sorted(_EXACT_CAPABILITIES))


def allowed_operator(rule: SignalRule, capability: StrategyFieldCapability) -> bool:
    """Return whether a signal operator belongs to the resolved capability."""

    return rule.operator in capability.allowed_operators

