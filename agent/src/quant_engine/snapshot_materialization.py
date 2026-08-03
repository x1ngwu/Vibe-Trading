"""Provider-neutral QE5 daily-market capture and snapshot publication.

The acquisition adapter is intentionally outside this module.  BaoStock can
populate :class:`Qe5MarketCapture` today; a future database reader can populate
the same contract without changing StrategySpec, the QUANTAXIS worker, or
persisted BacktestRun provenance.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    DataSnapshotRef,
    DEFAULT_HOUSEHOLD_COSTS,
    ResearchObject,
    ResearchSpec,
    canonical_json,
    canonical_sha256,
    create_research_object,
)
from src.research.store import ResearchStore

from .quantaxis_adapter import write_quantaxis_backtest_snapshot


CAPTURE_SCHEMA = "vibe.qe5-market-capture.v1"
MATERIALIZATION_SCHEMA = "vibe.qe5-materialization-manifest.v1"
UNIVERSE_MANIFEST_SCHEMA = "vibe.qe5-universe-manifest.v1"
RULE_TABLE_SCHEMA = "vibe.cn-equity-rule-table.v1"
TRANSITION_START_DATE = date(2023, 8, 28)
SHANGHAI = ZoneInfo("Asia/Shanghai")
_CANONICAL_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_main_board_symbol(
    symbol: str,
    board: Literal["sh_main", "sz_main"],
) -> None:
    code, separator, exchange = symbol.partition(".")
    if separator != "." or len(code) != 6 or not code.isdigit():
        raise ValueError("instrument must use a six-digit CODE.SH/CODE.SZ symbol")
    valid_prefixes = {
        "sh_main": ("600", "601", "603", "605"),
        "sz_main": ("000", "001", "002", "003"),
    }
    expected_exchange = {"sh_main": "SH", "sz_main": "SZ"}[board]
    if exchange != expected_exchange or not code.startswith(valid_prefixes[board]):
        raise ValueError(
            f"{board} instrument is outside the explicit main-board code set"
        )


class Qe5UniverseInstrument(_StrictModel):
    """Operator-reviewed board classification; providers must not infer it."""

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    board: Literal["sh_main", "sz_main"]

    @model_validator(mode="after")
    def validate_main_board_code(self) -> "Qe5UniverseInstrument":
        _validate_main_board_symbol(self.symbol, self.board)
        return self


class Qe5UniverseManifest(_StrictModel):
    schema_version: Literal[
        "vibe.qe5-universe-manifest.v1"
    ] = UNIVERSE_MANIFEST_SCHEMA
    universe_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    instruments: tuple[Qe5UniverseInstrument, ...] = Field(
        min_length=1,
        max_length=12,
    )

    @model_validator(mode="after")
    def validate_instruments(self) -> "Qe5UniverseManifest":
        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("universe instruments must be unique and symbol-sorted")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self)


class Qe5CapturedInstrument(_StrictModel):
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    board: Literal["sh_main", "sz_main"]
    listing_date: date
    delisting_date: date | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "Qe5CapturedInstrument":
        _validate_main_board_symbol(self.symbol, self.board)
        if self.delisting_date is not None and self.delisting_date < self.listing_date:
            raise ValueError("instrument delisting_date precedes listing_date")
        return self


class Qe5CapturedDailyBar(_StrictModel):
    trade_date: date
    known_at: AwareDatetime
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    open_fen: int = Field(gt=0)
    high_fen: int = Field(gt=0)
    low_fen: int = Field(gt=0)
    close_fen: int = Field(gt=0)
    signal_close_fen: int = Field(gt=0)
    limit_reference_fen: int = Field(gt=0)
    volume_shares: int = Field(ge=0)
    amount_fen: int = Field(ge=0)
    status: Literal["traded", "suspended"]
    is_st: bool
    listing_trade_day_number: int = Field(ge=6)

    @model_validator(mode="after")
    def validate_bar(self) -> "Qe5CapturedDailyBar":
        if self.known_at.astimezone(SHANGHAI).date() != self.trade_date:
            raise ValueError("daily bar known_at must be on its Shanghai trade date")
        if self.low_fen > min(self.open_fen, self.close_fen):
            raise ValueError("daily bar low exceeds open/close")
        if self.high_fen < max(self.open_fen, self.close_fen):
            raise ValueError("daily bar high is below open/close")
        if self.status == "suspended" and (
            self.volume_shares != 0 or self.amount_fen != 0
        ):
            raise ValueError("suspended bar must have zero volume and amount")
        return self


class Qe5CapturedCorporateAction(_StrictModel):
    action_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    kind: Literal["share_split", "cash_dividend"]
    known_at: AwareDatetime
    record_date: date
    ex_date: date
    pay_date: date
    multiplier_numerator: int = Field(gt=0)
    multiplier_denominator: int = Field(gt=0)
    cash_per_share_numerator_fen: int = Field(ge=0)
    cash_per_share_denominator: int = Field(gt=0)
    cash_rounding: Literal["none", "half_up_total_fen"]

    @model_validator(mode="after")
    def validate_action(self) -> "Qe5CapturedCorporateAction":
        if not self.record_date <= self.ex_date <= self.pay_date:
            raise ValueError("corporate action dates are reversed")
        if self.known_at.astimezone(SHANGHAI).date() > self.ex_date:
            raise ValueError("corporate action became known after its ex-date")
        if self.kind == "share_split":
            if (
                self.cash_per_share_numerator_fen != 0
                or self.cash_per_share_denominator != 1
                or self.cash_rounding != "none"
            ):
                raise ValueError("share split cannot carry cash")
            if self.multiplier_numerator <= self.multiplier_denominator:
                raise ValueError("share split multiplier must increase shares")
        elif (
            self.multiplier_numerator != 1
            or self.multiplier_denominator != 1
            or self.cash_per_share_numerator_fen <= 0
            or self.cash_rounding != "half_up_total_fen"
        ):
            raise ValueError("cash dividend requires 1/1 multiplier and positive cash")
        return self


class Qe5MarketCapture(_StrictModel):
    schema_version: Literal["vibe.qe5-market-capture.v1"] = CAPTURE_SCHEMA
    provider: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    provider_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    universe_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    universe_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: AwareDatetime
    as_of: date
    start_date: date
    end_date: date
    instruments: tuple[Qe5CapturedInstrument, ...] = Field(
        min_length=1,
        max_length=12,
    )
    calendar: tuple[date, ...] = Field(min_length=2, max_length=2_500)
    bars: tuple[Qe5CapturedDailyBar, ...] = Field(
        min_length=2,
        max_length=30_000,
    )
    corporate_actions: tuple[Qe5CapturedCorporateAction, ...] = Field(
        default=(),
        max_length=2_000,
    )
    anomalies: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_capture(self) -> "Qe5MarketCapture":
        if (
            self.start_date < TRANSITION_START_DATE
            or self.start_date > self.end_date
            or self.end_date > self.as_of
            or self.as_of > self.captured_at.astimezone(SHANGHAI).date()
        ):
            raise ValueError(
                "capture dates must satisfy transition_start <= start <= end "
                "<= as_of <= Shanghai capture date"
            )
        if self.calendar != tuple(sorted(set(self.calendar))):
            raise ValueError("calendar must be unique and sorted")
        if self.calendar[0] < self.start_date or self.calendar[-1] > self.end_date:
            raise ValueError("calendar lies outside the capture range")

        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("instruments must be unique and symbol-sorted")
        instrument_by_symbol = {item.symbol: item for item in self.instruments}
        for instrument in self.instruments:
            if instrument.listing_date >= self.start_date:
                raise ValueError(
                    "transitional materializer only accepts instruments listed "
                    "before the requested window"
                )

        expected_keys = tuple(
            (trade_date, symbol)
            for trade_date in self.calendar
            for symbol in symbols
        )
        actual_keys = tuple((item.trade_date, item.symbol) for item in self.bars)
        if actual_keys != expected_keys:
            raise ValueError(
                "bars must form one complete calendar x instrument matrix in "
                "date/symbol order"
            )
        for bar in self.bars:
            instrument = instrument_by_symbol.get(bar.symbol)
            if instrument is None:
                raise ValueError("bar references an unknown instrument")
            if (
                bar.trade_date < instrument.listing_date
                or (
                    instrument.delisting_date is not None
                    and bar.trade_date > instrument.delisting_date
                )
            ):
                raise ValueError("bar lies outside instrument lifecycle")

        action_ids: set[str] = set()
        action_order = tuple(
            (item.ex_date, item.action_id) for item in self.corporate_actions
        )
        if action_order != tuple(sorted(action_order)):
            raise ValueError("corporate actions must be ex-date/action-id sorted")
        open_dates = set(self.calendar)
        for action in self.corporate_actions:
            if action.action_id in action_ids:
                raise ValueError("duplicate corporate action ID")
            action_ids.add(action.action_id)
            if action.symbol not in instrument_by_symbol:
                raise ValueError("corporate action references an unknown instrument")
            if (
                action.record_date not in open_dates
                or action.ex_date not in open_dates
                or action.pay_date not in open_dates
            ):
                raise ValueError(
                    "transitional corporate action dates must be open dates "
                    "inside the capture"
                )
        if len(set(self.anomalies)) != len(self.anomalies):
            raise ValueError("capture anomalies must be unique")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self)


class Qe5MaterializationPolicy(_StrictModel):
    commission_tenths_bps: int = Field(
        default=int(DEFAULT_HOUSEHOLD_COSTS.commission_bps * 10),
        ge=0,
    )
    minimum_commission_fen: int = Field(
        default=int(DEFAULT_HOUSEHOLD_COSTS.minimum_commission * 100),
        ge=0,
    )
    sell_tax_tenths_bps: int = Field(
        default=int(DEFAULT_HOUSEHOLD_COSTS.sell_tax_bps * 10),
        ge=0,
    )
    transfer_fee_tenths_bps: int = Field(
        default=int(DEFAULT_HOUSEHOLD_COSTS.transfer_fee_bps * 10),
        ge=0,
    )
    fee_rule_version: str = Field(
        default=DEFAULT_HOUSEHOLD_COSTS.rule_version,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    max_participation_bps: int = Field(default=1_000, ge=1, le=10_000)


class Qe5MaterializationManifest(_StrictModel):
    schema_version: Literal[
        "vibe.qe5-materialization-manifest.v1"
    ] = MATERIALIZATION_SCHEMA
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_file: Literal["snapshot.json"] = "snapshot.json"
    research_spec_id: str = Field(pattern=r"^research_spec:[0-9a-f]{64}$")
    data_snapshot_id: str = Field(
        pattern=r"^data_snapshot_ref:[0-9a-f]{64}$"
    )
    provider: str
    provider_version: str
    universe_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_root: str | None = None


class Qe5MaterializationResult(_StrictModel):
    output_dir: Path
    snapshot_path: Path
    capture: Qe5MarketCapture
    snapshot_payload: dict[str, Any]
    research_object: ResearchObject
    data_snapshot_object: ResearchObject
    manifest: Qe5MaterializationManifest


def _market_rule_id(board: str, is_st: bool) -> str:
    suffix = "st-5pct" if is_st else "standard-10pct"
    return f"{board}-{suffix}-post-20230828-v1"


def build_qe5_snapshot_payload(
    capture: Qe5MarketCapture,
    *,
    policy: Qe5MaterializationPolicy | None = None,
) -> dict[str, Any]:
    """Convert a validated provider-neutral capture into exact worker input."""

    selected = policy or Qe5MaterializationPolicy()
    dividend_windows: dict[str, list[tuple[date, date]]] = {}
    for action in capture.corporate_actions:
        if action.kind != "cash_dividend":
            continue
        windows = dividend_windows.setdefault(action.symbol, [])
        if any(
            not (
                action.pay_date < existing_ex
                or action.ex_date > existing_pay
            )
            for existing_ex, existing_pay in windows
        ):
            raise ValueError(
                "overlapping cash-dividend receivables are unsupported"
            )
        windows.append((action.ex_date, action.pay_date))
    rules = []
    for board in ("sh_main", "sz_main"):
        for is_st, limit_bps in ((False, 1_000), (True, 500)):
            rules.append(
                {
                    "rule_id": _market_rule_id(board, is_st),
                    "effective_from": capture.start_date.isoformat(),
                    "effective_to": capture.end_date.isoformat(),
                    "board": board,
                    "is_st": is_st,
                    "listing_day_min": 6,
                    "listing_day_max": None,
                    "limit_up_bps": limit_bps,
                    "limit_down_bps": limit_bps,
                    "tick_fen": 1,
                }
            )
    return {
        "schema_version": "vibe.quantaxis-backtest-snapshot.v1",
        "price_semantics": {
            "execution_price_adjustment": "raw",
            "signal_price_adjustment": "qfq",
            "corporate_action_mode": "explicit",
        },
        "rule_table": {
            "schema_version": RULE_TABLE_SCHEMA,
            "version": (
                "cn-equity-mainboard-post-20230828-"
                f"{canonical_sha256(selected)[:16]}"
            ),
            "fee_schedule": {
                "effective_from": capture.start_date.isoformat(),
                "effective_to": capture.end_date.isoformat(),
                "commission_tenths_bps": selected.commission_tenths_bps,
                "minimum_commission_fen": selected.minimum_commission_fen,
                "sell_tax_tenths_bps": selected.sell_tax_tenths_bps,
                "transfer_fee_tenths_bps": selected.transfer_fee_tenths_bps,
                "rule_version": selected.fee_rule_version,
            },
            "market_rules": rules,
            "max_participation_bps": selected.max_participation_bps,
        },
        "instruments": [
            {
                "symbol": item.symbol,
                "board": item.board,
                "listing_date": item.listing_date.isoformat(),
                "delisting_date": (
                    item.delisting_date.isoformat()
                    if item.delisting_date is not None
                    else None
                ),
            }
            for item in capture.instruments
        ],
        "calendar": [
            {"trade_date": item.isoformat(), "is_open": True}
            for item in capture.calendar
        ],
        "bars": [
            {
                "trade_date": item.trade_date.isoformat(),
                "known_at": item.known_at.isoformat(),
                "symbol": item.symbol,
                "open_fen": item.open_fen,
                "high_fen": item.high_fen,
                "low_fen": item.low_fen,
                "close_fen": item.close_fen,
                "signal_close_fen": item.signal_close_fen,
                "limit_reference_fen": item.limit_reference_fen,
                "volume_shares": item.volume_shares,
                "status": item.status,
                "is_st": item.is_st,
                "listing_trade_day_number": item.listing_trade_day_number,
                "market_rule_id": _market_rule_id(
                    next(
                        instrument.board
                        for instrument in capture.instruments
                        if instrument.symbol == item.symbol
                    ),
                    item.is_st,
                ),
                "features": {"amount_fen": item.amount_fen},
            }
            for item in capture.bars
        ],
        "corporate_actions": [
            item.model_dump(mode="json") for item in capture.corporate_actions
        ],
    }


def _write_json(path: Path, value: Any) -> None:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _has_symlink_component(path: Path) -> bool:
    current = path.absolute()
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def materialize_qe5_capture(
    capture: Qe5MarketCapture,
    output_dir: Path,
    *,
    policy: Qe5MaterializationPolicy | None = None,
    publish_root: Path | None = None,
) -> Qe5MaterializationResult:
    """Create an auditable bundle and optionally publish it to one Vibe home."""

    target = Path(output_dir).absolute()
    if target.exists() or target.is_symlink() or _has_symlink_component(target.parent):
        raise ValueError(f"refusing to overwrite materialization output: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(target):
        raise ValueError("materialization output path must not contain a symlink")
    publish_target = Path(publish_root).absolute() if publish_root is not None else None
    if publish_target is not None and _has_symlink_component(publish_target):
        raise ValueError("publish root must not contain a symlink")
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    os.chmod(temporary, 0o700)
    snapshot_payload = build_qe5_snapshot_payload(capture, policy=policy)
    try:
        snapshot_sha256 = write_quantaxis_backtest_snapshot(
            snapshot_payload,
            temporary / "snapshot.json",
        )
        research = create_research_object(
            ResearchSpec(
                symbols=tuple(item.symbol for item in capture.instruments),
                as_of=capture.as_of,
                lookback_days=(
                    min((capture.end_date - capture.start_date).days + 1, 10_000),
                ),
                candidate_universe=capture.universe_id,
                requested_outputs=("strategy", "backtest"),
            ),
            created_at=capture.captured_at,
        )
        source = f"{capture.provider}-{capture.provider_version}"
        anomalies = tuple(
            dict.fromkeys(
                (
                    *capture.anomalies,
                    f"capture_schema:{CAPTURE_SCHEMA}",
                    "amount_carried_as_bar_feature:amount_fen",
                )
            )
        )
        data_snapshot = create_research_object(
            DataSnapshotRef(
                snapshot_sha256=snapshot_sha256,
                as_of=capture.as_of,
                start_date=capture.start_date,
                end_date=capture.end_date,
                adjustment="qfq",
                symbols=tuple(item.symbol for item in capture.instruments),
                fields=_CANONICAL_FIELDS,
                requested_sources=(capture.provider,),
                actual_sources={
                    item.symbol: source for item in capture.instruments
                },
                anomalies=anomalies,
            ),
            parent_refs=(research.ref(),),
            created_at=capture.captured_at,
        )
        if publish_target is not None:
            published_snapshot = (
                publish_target
                / "backtest-snapshots"
                / f"{snapshot_sha256}.json"
            )
            write_quantaxis_backtest_snapshot(
                snapshot_payload,
                published_snapshot,
            )
            store = ResearchStore(publish_target / "research")
            store.put(research)
            store.put(data_snapshot)
        published_text = str(publish_target) if publish_target is not None else None
        manifest = Qe5MaterializationManifest(
            capture_sha256=capture.content_sha256,
            snapshot_sha256=snapshot_sha256,
            research_spec_id=research.object_id,
            data_snapshot_id=data_snapshot.object_id,
            provider=capture.provider,
            provider_version=capture.provider_version,
            universe_manifest_sha256=capture.universe_manifest_sha256,
            published_root=published_text,
        )
        _write_json(temporary / "capture.json", capture)
        _write_json(temporary / "research-spec.json", research)
        _write_json(temporary / "data-snapshot-ref.json", data_snapshot)
        _write_json(temporary / "manifest.json", manifest)
        directory_descriptor = os.open(
            temporary,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        temporary.rename(target)
        parent_descriptor = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return Qe5MaterializationResult(
        output_dir=target,
        snapshot_path=target / "snapshot.json",
        capture=capture,
        snapshot_payload=snapshot_payload,
        research_object=research,
        data_snapshot_object=data_snapshot,
        manifest=manifest,
    )
