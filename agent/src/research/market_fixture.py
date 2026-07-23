"""Strict, offline-only QE1 market fixture contracts and readers."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .contracts import DataSnapshotRef, canonical_json, canonical_sha256

MARKET_FIXTURE_SCHEMA = "vibe.market-fixture.v1"


class MarketFixtureError(ValueError):
    """Raised when a frozen market fixture is ambiguous or incomplete."""


class _StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdjustmentRules(_StrictFixtureModel):
    raw_price: Literal["unadjusted_exchange_price"]
    qfq_anchor: Literal["last_close_equals_raw"]
    hfq_anchor: Literal["first_close_equals_raw"]
    cumulative_factor: Literal["product_of_single_event_factors"]
    single_event_factor: Literal["post_event_theoretical_price/pre_event_close"]
    price: Literal["multiply_by_relative_cumulative_factor"]
    volume: Literal["divide_by_share_multiplier_only"]
    amount: Literal["never_adjust"]
    shares: Literal["multiply_by_share_multiplier"]


class MarketInstrument(_StrictFixtureModel):
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    instrument_type: Literal["stock", "etf", "index"]
    board: Literal["sh_main", "sz_main", "chinext", "star", "etf", "index"]
    listing_date: date
    delisting_date: date | None = None
    scenarios: tuple[
        Literal[
            "ordinary",
            "cash_dividend",
            "share_change",
            "rights_event",
            "suspension",
            "true_missing",
            "duplicate_same",
            "duplicate_conflict",
            "listing_boundary",
            "delisting_boundary",
            "st",
            "limit_up_locked",
            "limit_down_locked",
            "capacity_shortfall",
            "synthetic_near",
            "synthetic_reverse",
            "missing_feature",
            "extreme_outlier",
        ],
        ...,
    ]


class CalendarDay(_StrictFixtureModel):
    trade_date: date
    is_open: bool
    reason: Literal["trading_day", "weekend", "holiday"]


class RawBar(_StrictFixtureModel):
    source_row_id: str = Field(min_length=1)
    symbol: str
    trade_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    amount: float = Field(ge=0)
    status: Literal["traded", "suspended"]
    available_at: AwareDatetime

    @model_validator(mode="after")
    def validate_ohlc(self) -> "RawBar":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the greatest OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the least OHLC value")
        if self.status == "suspended" and (self.volume != 0 or self.amount != 0):
            raise ValueError("suspended bars must have zero volume and amount")
        return self


class CorporateAction(_StrictFixtureModel):
    event_id: str
    symbol: str
    kind: Literal["cash_dividend", "share_split", "rights_issue"]
    announcement_date: date
    record_date: date
    ex_date: date
    pay_date: date
    status: Literal["implemented", "cancelled"]
    cash_per_share: float = Field(ge=0)
    share_multiplier: float = Field(gt=0)
    rights_ratio: float = Field(ge=0)
    rights_price: float | None = Field(default=None, gt=0)
    single_event_factor: float = Field(gt=0)
    cumulative_factor_after: float = Field(gt=0)


class PointInTimeMetadata(_StrictFixtureModel):
    symbol: str
    field: Literal["industry", "index_member", "is_st", "financial_metric"]
    value: str | bool | float
    effective_date: date
    known_at: AwareDatetime


class MissingExpectation(_StrictFixtureModel):
    symbol: str
    trade_date: date
    classification: Literal["suspension", "true_missing", "not_listed", "delisted"]


class DedupExpectation(_StrictFixtureModel):
    symbol: str
    trade_date: date
    outcome: Literal["deduplicate", "reject_conflict"]
    conflicting_fields: tuple[str, ...] = ()


class LimitExpectation(_StrictFixtureModel):
    symbol: str
    trade_date: date
    rule: Literal["main_10pct", "st_5pct", "chinext_20pct", "star_20pct"]
    order_side: Literal["buy", "sell"]
    outcome: Literal["filled", "rejected_locked_limit", "partial_capacity"]
    maximum_fill_shares: int = Field(ge=0)


class SimilarityExpectation(_StrictFixtureModel):
    symbol: str
    relation: Literal["near", "reverse", "missing", "outlier"]
    feature_value: float | None


class HandCalculatedExpected(_StrictFixtureModel):
    missing: tuple[MissingExpectation, ...]
    deduplication: tuple[DedupExpectation, ...]
    execution_limits: tuple[LimitExpectation, ...]
    similarity_samples: tuple[SimilarityExpectation, ...]


class MarketFixture(_StrictFixtureModel):
    schema_version: Literal["vibe.market-fixture.v1"] = MARKET_FIXTURE_SCHEMA
    fixture_id: Literal["qe1-cn-small-market-v1"]
    as_of: date
    currency: Literal["CNY"]
    source: Literal["synthetic-offline-fixture"]
    adjustment_rules: AdjustmentRules
    instruments: tuple[MarketInstrument, ...]
    calendar: tuple[CalendarDay, ...]
    raw_bars: tuple[RawBar, ...]
    corporate_actions: tuple[CorporateAction, ...]
    point_in_time_metadata: tuple[PointInTimeMetadata, ...]
    expected: HandCalculatedExpected

    @model_validator(mode="after")
    def validate_market_coverage(self) -> "MarketFixture":
        symbols = [item.symbol for item in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("instrument symbols must be unique")
        counts = {
            kind: sum(item.instrument_type == kind for item in self.instruments)
            for kind in ("stock", "etf", "index")
        }
        if not 6 <= counts["stock"] <= 12 or counts["etf"] != 1 or counts["index"] != 1:
            raise ValueError("fixture requires 6-12 stocks, one ETF, and one index")
        declared = set(symbols)
        referenced = {
            *(item.symbol for item in self.raw_bars),
            *(item.symbol for item in self.corporate_actions),
            *(item.symbol for item in self.point_in_time_metadata),
        }
        if referenced - declared:
            raise ValueError("fixture data references an undeclared symbol")
        scenarios = {scenario for item in self.instruments for scenario in item.scenarios}
        required = set(MarketInstrument.model_fields["scenarios"].annotation.__args__[0].__args__)
        if scenarios != required:
            missing = sorted(required - scenarios)
            extra = sorted(scenarios - required)
            raise ValueError(f"fixture scenario coverage mismatch: missing={missing}, extra={extra}")
        return self

    @property
    def fixture_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def snapshot_sha256(self) -> str:
        """Hash only information visible by the fixture's fixed ``as_of`` date."""

        visible = {
            "schema_version": self.schema_version,
            "fixture_id": self.fixture_id,
            "as_of": self.as_of,
            "currency": self.currency,
            "source": self.source,
            "adjustment_rules": self.adjustment_rules,
            "instruments": self.instruments,
            "calendar": tuple(day for day in self.calendar if day.trade_date <= self.as_of),
            "raw_bars": tuple(
                row
                for row in self.raw_bars
                if row.trade_date <= self.as_of and row.available_at.date() <= self.as_of
            ),
            "corporate_actions": tuple(
                action
                for action in self.corporate_actions
                if action.announcement_date <= self.as_of
            ),
            "point_in_time_metadata": tuple(
                item
                for item in self.point_in_time_metadata
                if item.known_at.date() <= self.as_of
            ),
        }
        return canonical_sha256(visible)

    def snapshot_ref(self) -> DataSnapshotRef:
        symbols = tuple(item.symbol for item in self.instruments)
        return DataSnapshotRef(
            snapshot_sha256=self.snapshot_sha256,
            as_of=self.as_of,
            start_date=min(day.trade_date for day in self.calendar),
            end_date=max(day.trade_date for day in self.calendar if day.trade_date <= self.as_of),
            adjustment="raw",
            symbols=symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in symbols},
            anomalies=("duplicate_same", "duplicate_conflict", "true_missing"),
        )


def load_market_fixture(path: Path) -> MarketFixture:
    """Read a fixture from a regular local file; this function never accesses a network."""

    fixture_path = Path(path)
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise MarketFixtureError("market fixture must be a regular, non-symlink file")
    return MarketFixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))


def normalize_raw_bars(rows: tuple[RawBar, ...]) -> tuple[RawBar, ...]:
    """Deterministically collapse identical duplicates and reject conflicting ones."""

    grouped: dict[tuple[str, date], list[RawBar]] = defaultdict(list)
    for row in rows:
        grouped[(row.symbol, row.trade_date)].append(row)
    normalized: list[RawBar] = []
    for key, candidates in sorted(grouped.items()):
        material = [candidate.model_dump(exclude={"source_row_id"}) for candidate in candidates]
        first = canonical_json(material[0])
        if any(canonical_json(item) != first for item in material[1:]):
            fields = sorted(
                field
                for field in material[0]
                if any(item[field] != material[0][field] for item in material[1:])
            )
            raise MarketFixtureError(
                f"conflicting duplicate for {key[0]} on {key[1].isoformat()}: fields={fields}"
            )
        normalized.append(min(candidates, key=lambda item: item.source_row_id))
    return tuple(normalized)


def point_in_time_value(
    fixture: MarketFixture,
    *,
    symbol: str,
    field: str,
    as_of: datetime,
) -> str | bool | float | None:
    """Return the latest value visible by ``as_of``, respecting known-at timestamps."""

    candidates = [
        item
        for item in fixture.point_in_time_metadata
        if item.symbol == symbol
        and item.field == field
        and item.effective_date <= as_of.date()
        and item.known_at <= as_of
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.known_at, item.effective_date)).value
