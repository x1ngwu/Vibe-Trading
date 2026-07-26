"""Strict Tushare source batches for content-bound CSI300 universes.

Tushare exposes CSI300 membership through ``index_weight`` using provider
identifier ``399300.SZ`` while the research contract uses canonical index
symbol ``000300.SH``.  A materializable batch joins each weight row to the
corresponding ``stock_basic`` lifecycle fields, records when the joined source
was captured, and refuses to treat a later capture as knowledge available at a
past research cutoff.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .contracts import canonical_sha256
from .universe import (
    PointInTimeUniverse,
    UniverseError,
    UniverseInstrument,
    UniverseMembership,
)

CSI300_SOURCE_BATCH_SCHEMA = "vibe.csi300-source-batch.v1"
CSI300_EXPECTED_CONSTITUENTS = 300
CSI300_TUSHARE_INDEX_CODE = "399300.SZ"
CSI300_CANONICAL_INDEX_SYMBOL = "000300.SH"


class _StrictSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Csi300TushareConstituent(_StrictSourceModel):
    """One exact ``index_weight`` row joined to ``stock_basic`` metadata."""

    index_code: Literal["399300.SZ"]
    con_code: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ)$")
    trade_date: date
    weight: Decimal = Field(gt=Decimal("0"), le=Decimal("100"))
    stock_basic_ts_code: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ)$")
    exchange: Literal["SSE", "SZSE"]
    list_status: Literal["L"]
    list_date: date
    delist_date: date | None = None

    @model_validator(mode="after")
    def validate_join(self) -> "Csi300TushareConstituent":
        if self.stock_basic_ts_code != self.con_code:
            raise ValueError("stock_basic_ts_code must match index_weight con_code")
        expected_exchange = "SSE" if self.con_code.endswith(".SH") else "SZSE"
        if self.exchange != expected_exchange:
            raise ValueError(
                f"exchange {self.exchange} does not match symbol {self.con_code}"
            )
        if self.list_date > self.trade_date:
            raise ValueError("constituent must be listed by index_weight trade_date")
        if self.delist_date is not None and self.delist_date < self.list_date:
            raise ValueError("delist_date must not precede list_date")
        return self


class Csi300TushareSourceBatch(_StrictSourceModel):
    """A complete, order-normalized Tushare source capture for one as-of date."""

    schema_version: Literal["vibe.csi300-source-batch.v1"] = (
        CSI300_SOURCE_BATCH_SCHEMA
    )
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    source: Literal["tushare"] = "tushare"
    source_version: str = Field(
        min_length=1,
        max_length=48,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    provider_index_code: Literal["399300.SZ"] = CSI300_TUSHARE_INDEX_CODE
    canonical_index_symbol: Literal["000300.SH"] = (
        CSI300_CANONICAL_INDEX_SYMBOL
    )
    as_of: date
    captured_at: AwareDatetime
    constituents: tuple[Csi300TushareConstituent, ...] = Field(min_length=1)

    @field_validator("constituents")
    @classmethod
    def normalize_constituent_order(
        cls,
        values: tuple[Csi300TushareConstituent, ...],
    ) -> tuple[Csi300TushareConstituent, ...]:
        return tuple(sorted(values, key=lambda item: item.con_code))

    @model_validator(mode="after")
    def validate_complete_capture(self) -> "Csi300TushareSourceBatch":
        if len(self.constituents) != CSI300_EXPECTED_CONSTITUENTS:
            raise ValueError(
                "CSI300 source batch must contain exactly "
                f"{CSI300_EXPECTED_CONSTITUENTS} constituents"
            )

        codes = [item.con_code for item in self.constituents]
        if len(codes) != len(set(codes)):
            raise ValueError("CSI300 source batch contains duplicate con_code values")

        trade_dates = {item.trade_date for item in self.constituents}
        if len(trade_dates) != 1:
            raise ValueError("all index_weight rows must share one trade_date")
        trade_date = next(iter(trade_dates))
        if trade_date > self.as_of:
            raise ValueError("index_weight trade_date must not exceed as_of")

        capture_date = self.captured_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date()
        if capture_date < trade_date:
            raise ValueError("captured_at must not precede index_weight trade_date")
        if capture_date > self.as_of:
            raise ValueError("a future source capture cannot backfill a past as_of")

        invalid_delistings = [
            item.con_code
            for item in self.constituents
            if item.delist_date is not None and item.delist_date <= self.as_of
        ]
        if invalid_delistings:
            raise ValueError(
                "source batch contains securities delisted by as_of: "
                + ",".join(invalid_delistings)
            )

        total_weight = sum(
            (item.weight for item in self.constituents),
            start=Decimal("0"),
        )
        if not Decimal("99") <= total_weight <= Decimal("101"):
            raise ValueError(
                f"CSI300 source weights must close near 100, got {total_weight}"
            )
        return self

    @property
    def source_content_sha256(self) -> str:
        """Hash the normalized semantic source content, independent of row order."""

        return canonical_sha256(self)

    @property
    def index_weight_trade_date(self) -> date:
        return self.constituents[0].trade_date


def _required_text(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise UniverseError(f"Tushare field {field_name} must be a non-empty string")
    return value.strip()


def _source_date(value: Any, field_name: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        normalized = value.strip().replace("-", "")
        if len(normalized) == 8 and normalized.isdigit():
            try:
                return date(
                    int(normalized[:4]),
                    int(normalized[4:6]),
                    int(normalized[6:]),
                )
            except ValueError as exc:
                raise UniverseError(
                    f"Tushare field {field_name} contains an invalid date"
                ) from exc
    raise UniverseError(f"Tushare field {field_name} must be YYYYMMDD")


def _optional_source_date(value: Any, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return _source_date(value, field_name)


def build_csi300_tushare_source_batch(
    index_weight_rows: Sequence[Mapping[str, Any]],
    stock_basic_rows: Sequence[Mapping[str, Any]],
    *,
    batch_id: str,
    source_version: str,
    as_of: date,
    captured_at: AwareDatetime,
) -> Csi300TushareSourceBatch:
    """Join exact raw Tushare exports without choosing a silent latest row."""

    stock_by_code: dict[str, Mapping[str, Any]] = {}
    for row in stock_basic_rows:
        ts_code = _required_text(row, "ts_code")
        if ts_code in stock_by_code:
            raise UniverseError(f"duplicate stock_basic ts_code: {ts_code}")
        stock_by_code[ts_code] = row

    constituents: list[Csi300TushareConstituent] = []
    for weight_row in index_weight_rows:
        con_code = _required_text(weight_row, "con_code")
        stock_row = stock_by_code.get(con_code)
        if stock_row is None:
            raise UniverseError(f"stock_basic metadata missing for {con_code}")
        try:
            weight = Decimal(str(weight_row.get("weight")))
        except Exception as exc:
            raise UniverseError("Tushare field weight must be a decimal number") from exc
        constituents.append(
            Csi300TushareConstituent(
                index_code=_required_text(weight_row, "index_code"),
                con_code=con_code,
                trade_date=_source_date(
                    weight_row.get("trade_date"),
                    "trade_date",
                ),
                weight=weight,
                stock_basic_ts_code=_required_text(stock_row, "ts_code"),
                exchange=_required_text(stock_row, "exchange"),
                list_status=_required_text(stock_row, "list_status"),
                list_date=_source_date(stock_row.get("list_date"), "list_date"),
                delist_date=_optional_source_date(
                    stock_row.get("delist_date"),
                    "delist_date",
                ),
            )
        )
    return Csi300TushareSourceBatch(
        batch_id=batch_id,
        source_version=source_version,
        as_of=as_of,
        captured_at=captured_at,
        constituents=tuple(constituents),
    )


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    return any(part.is_symlink() for part in (candidate, *candidate.parents))


def load_csi300_tushare_source_batch(path: Path) -> Csi300TushareSourceBatch:
    """Read a strict, local source export without network access."""

    source_path = Path(path)
    if _has_symlink_component(source_path) or not source_path.is_file():
        raise UniverseError("CSI300 source batch must be a regular, non-symlink file")
    try:
        return Csi300TushareSourceBatch.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise UniverseError(f"invalid CSI300 source batch: {exc}") from exc


def materialize_csi300_tushare_universe(
    batch: Csi300TushareSourceBatch,
) -> PointInTimeUniverse:
    """Convert one complete source capture into a fixed point-in-time universe."""

    source_hash = batch.source_content_sha256
    bound_source_version = f"{batch.source_version}+sha256.{source_hash}"
    if len(bound_source_version) > 128:
        raise UniverseError("content-bound source_version exceeds universe contract")

    instruments = tuple(
        UniverseInstrument(
            security_id=f"TS:{item.stock_basic_ts_code}",
            symbol=item.con_code,
            instrument_type="stock",
            listing_date=item.list_date,
            delisting_date=item.delist_date,
            primary_listing=True,
            known_at=batch.captured_at,
        )
        for item in batch.constituents
    )
    memberships = tuple(
        UniverseMembership(
            source_row_id=(
                f"tushare:index_weight:{item.index_code}:"
                f"{item.trade_date:%Y%m%d}:{item.con_code}"
            ),
            symbol=item.con_code,
            effective_from=item.trade_date,
            effective_to=batch.as_of,
            known_at=batch.captured_at,
        )
        for item in batch.constituents
    )
    universe = PointInTimeUniverse(
        fixture_id=(
            f"csi300-{batch.as_of.isoformat()}-tushare-{source_hash[:16]}"
        ),
        universe_id="csi300",
        index_symbol=batch.canonical_index_symbol,
        source=batch.source,
        source_version=bound_source_version,
        instruments=instruments,
        memberships=memberships,
    )
    snapshot = universe.snapshot(batch.as_of)
    if len(snapshot.active_symbols) != CSI300_EXPECTED_CONSTITUENTS:
        raise UniverseError("materialized CSI300 snapshot is not exactly 300 members")
    return universe
