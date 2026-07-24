"""Content-bound stock price adjustment for the QE2 data envelope.

Providers remain responsible for returning raw exchange bars.  This module
derives qfq/hfq frames from an independently versioned factor context so a
fallback can never silently change the adjustment convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import canonical_sha256


class AdjustmentError(ValueError):
    """Raised when adjustment inputs are incomplete or internally inconsistent."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdjustmentFactorPoint(_StrictModel):
    """Point-in-time factor values for one trading date.

    ``price_factor`` follows the Tushare convention: qfq prices multiply by
    ``factor_t / factor_end`` and hfq prices by ``factor_t / factor_start``.
    ``share_factor`` is cumulative outstanding-share scaling and is used only
    for volume; cash dividends therefore never alter volume.
    """

    trade_date: date
    price_factor: float = Field(gt=0)
    share_factor: float = Field(gt=0)
    known_at: AwareDatetime


class AdjustmentContext(_StrictModel):
    """Immutable factor series whose content hash becomes adjustment provenance."""

    schema_version: Literal["vibe.adjustment-context.v1"] = "vibe.adjustment-context.v1"
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=128)
    as_of: date
    points: tuple[AdjustmentFactorPoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_points(self) -> "AdjustmentContext":
        dates = [item.trade_date for item in self.points]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("adjustment factor dates must be unique and sorted")
        if any(item.trade_date > self.as_of for item in self.points):
            raise ValueError("adjustment factors must not extend beyond as_of")
        if any(item.known_at.date() > self.as_of for item in self.points):
            raise ValueError("adjustment factors must be known by as_of")
        return self

    @property
    def context_sha256(self) -> str:
        return canonical_sha256(self)


class AdjustmentManifest(_StrictModel):
    schema_version: Literal["vibe.price-adjustment.v1"] = "vibe.price-adjustment.v1"
    symbol: str
    adjustment: Literal["raw", "qfq", "hfq"]
    factor_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_anchor_date: date | None
    volume_anchor_date: date | None
    amount_adjustment: Literal["none"] = "none"


@dataclass(frozen=True)
class AdjustedFrame:
    frame: pd.DataFrame
    manifest: AdjustmentManifest


def adjust_stock_frame(
    frame: pd.DataFrame,
    *,
    adjustment: Literal["raw", "qfq", "hfq"],
    context: AdjustmentContext,
) -> AdjustedFrame:
    """Derive a stock frame without mutating raw bars.

    Prices use the factor ratio at the selected anchor.  Volume is expressed
    on the anchor-date share basis; amount is never adjusted.  Every bar date
    requires an exact factor observation so missing factors fail closed.
    """

    required = {"open", "high", "low", "close", "volume", "amount"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise AdjustmentError(f"raw frame is missing required fields: {missing}")
    if frame.empty:
        raise AdjustmentError("raw frame must not be empty")

    normalized = frame.loc[:, ["open", "high", "low", "close", "volume", "amount"]].copy()
    try:
        normalized.index = pd.to_datetime(normalized.index)
    except Exception as exc:  # noqa: BLE001
        raise AdjustmentError("raw frame has an invalid date index") from exc
    if normalized.index.has_duplicates:
        raise AdjustmentError("raw frame dates must be unique")
    normalized = normalized.sort_index(kind="stable")
    normalized.index.name = "trade_date"
    bar_dates = tuple(item.date() for item in normalized.index)
    if any(item > context.as_of for item in bar_dates):
        raise AdjustmentError("raw frame extends beyond adjustment context as_of")

    points = {item.trade_date: item for item in context.points}
    missing_factors = [item.isoformat() for item in bar_dates if item not in points]
    if missing_factors:
        raise AdjustmentError(f"adjustment factors do not cover bar dates: {missing_factors}")

    input_sha256 = _frame_sha256(normalized)
    result = normalized.copy()
    anchor_date: date | None = None
    if adjustment != "raw":
        anchor_date = bar_dates[-1] if adjustment == "qfq" else bar_dates[0]
        anchor = points[anchor_date]
        price_multipliers = pd.Series(
            [points[item].price_factor / anchor.price_factor for item in bar_dates],
            index=result.index,
            dtype=float,
        )
        volume_multipliers = pd.Series(
            [anchor.share_factor / points[item].share_factor for item in bar_dates],
            index=result.index,
            dtype=float,
        )
        for field in ("open", "high", "low", "close"):
            result[field] = pd.to_numeric(result[field], errors="raise") * price_multipliers
        result["volume"] = pd.to_numeric(result["volume"], errors="raise") * volume_multipliers
    result["amount"] = normalized["amount"]

    manifest = AdjustmentManifest(
        symbol=context.symbol,
        adjustment=adjustment,
        factor_context_sha256=context.context_sha256,
        input_sha256=input_sha256,
        output_sha256=_frame_sha256(result),
        price_anchor_date=anchor_date,
        volume_anchor_date=anchor_date,
    )
    return AdjustedFrame(frame=result, manifest=manifest)


def _frame_sha256(frame: pd.DataFrame) -> str:
    rows = []
    for trade_date, row in frame.iterrows():
        rows.append(
            {
                "trade_date": pd.Timestamp(trade_date).date().isoformat(),
                **{field: float(row[field]) for field in frame.columns},
            }
        )
    return canonical_sha256(rows)
