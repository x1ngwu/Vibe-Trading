"""BaoStock acquisition adapter for the provider-neutral QE5 capture contract."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import hashlib
from typing import Any, Iterable, Mapping

from .snapshot_materialization import (
    SHANGHAI,
    TRANSITION_START_DATE,
    Qe5CapturedCorporateAction,
    Qe5CapturedDailyBar,
    Qe5CapturedInstrument,
    Qe5MarketCapture,
    Qe5UniverseManifest,
)


class BaoStockCaptureError(RuntimeError):
    """Stable fail-closed error for provider or normalization problems."""


_HISTORY_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "tradestatus",
    "isST",
)


def _provider_code(symbol: str) -> str:
    code, suffix = symbol.split(".", 1)
    if suffix == "SH":
        return f"sh.{code}"
    if suffix == "SZ":
        return f"sz.{code}"
    raise BaoStockCaptureError(f"unsupported BaoStock symbol: {symbol}")


def _canonical_symbol(code: str) -> str:
    value = code.strip().lower()
    if value.startswith("sh.") and len(value) == 9:
        return f"{value[3:]}.SH"
    if value.startswith("sz.") and len(value) == 9:
        return f"{value[3:]}.SZ"
    raise BaoStockCaptureError(f"BaoStock returned an invalid code: {code!r}")


def _rows(result: Any, *, label: str) -> list[dict[str, str]]:
    if getattr(result, "error_code", None) != "0":
        raise BaoStockCaptureError(
            f"{label} failed: {getattr(result, 'error_msg', 'unknown provider error')}"
        )
    fields = tuple(getattr(result, "fields", ()) or ())
    if not fields or len(fields) != len(set(fields)):
        raise BaoStockCaptureError(f"{label} returned invalid fields")
    output: list[dict[str, str]] = []
    while result.next():
        values = result.get_row_data()
        if len(values) != len(fields):
            raise BaoStockCaptureError(f"{label} returned a malformed row")
        output.append(dict(zip(fields, values, strict=True)))
    return output


def _required(row: Mapping[str, str], name: str, label: str) -> str:
    value = str(row.get(name, "")).strip()
    if not value:
        raise BaoStockCaptureError(f"{label} omitted {name}")
    return value


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BaoStockCaptureError(f"{label} is not YYYY-MM-DD") from exc


def _decimal(value: str, label: str, *, blank_zero: bool = False) -> Decimal:
    text = value.strip()
    if blank_zero and not text:
        return Decimal(0)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise BaoStockCaptureError(f"{label} is not decimal") from exc
    if not result.is_finite():
        raise BaoStockCaptureError(f"{label} is not finite")
    return result


def _exact_fen(value: str, label: str) -> int:
    scaled = _decimal(value, label) * 100
    if scaled != scaled.to_integral_value():
        raise BaoStockCaptureError(f"{label} cannot be represented in integer fen")
    result = int(scaled)
    if result <= 0:
        raise BaoStockCaptureError(f"{label} must be positive")
    return result


def _rounded_fen(
    value: str,
    label: str,
    *,
    blank_zero: bool = False,
) -> int:
    scaled = (_decimal(value, label, blank_zero=blank_zero) * 100).quantize(
        Decimal(1),
        rounding=ROUND_HALF_UP,
    )
    result = int(scaled)
    if result < 0:
        raise BaoStockCaptureError(f"{label} must not be negative")
    return result


def _integer(value: str, label: str, *, blank_zero: bool = False) -> int:
    text = value.strip()
    if blank_zero and not text:
        return 0
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise BaoStockCaptureError(f"{label} is not an integer") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise BaoStockCaptureError(f"{label} is not an integer")
    result = int(parsed)
    if result < 0:
        raise BaoStockCaptureError(f"{label} must not be negative")
    return result


def _history_by_date(
    rows: Iterable[Mapping[str, str]],
    *,
    symbol: str,
    adjustflag: str,
) -> dict[date, Mapping[str, str]]:
    output: dict[date, Mapping[str, str]] = {}
    for row in rows:
        label = f"{symbol} adjustflag={adjustflag}"
        if _canonical_symbol(_required(row, "code", label)) != symbol:
            raise BaoStockCaptureError(f"{label} returned another symbol")
        if _required(row, "adjustflag", label) != adjustflag:
            raise BaoStockCaptureError(f"{label} returned another adjustment")
        trade_date = _parse_date(_required(row, "date", label), f"{label} date")
        if trade_date in output:
            raise BaoStockCaptureError(f"{label} returned duplicate dates")
        status = _required(row, "tradestatus", label)
        is_st = _required(row, "isST", label)
        if status not in {"0", "1"} or is_st not in {"0", "1"}:
            raise BaoStockCaptureError(f"{label} returned invalid status flags")
        output[trade_date] = row
    return output


def _listing_metadata(
    provider: Any,
    manifest: Qe5UniverseManifest,
) -> tuple[Qe5CapturedInstrument, ...]:
    output: list[Qe5CapturedInstrument] = []
    for item in manifest.instruments:
        rows = _rows(
            provider.query_stock_basic(code=_provider_code(item.symbol)),
            label=f"query_stock_basic:{item.symbol}",
        )
        if len(rows) != 1:
            raise BaoStockCaptureError(
                f"query_stock_basic:{item.symbol} returned {len(rows)} rows"
            )
        row = rows[0]
        if _canonical_symbol(_required(row, "code", item.symbol)) != item.symbol:
            raise BaoStockCaptureError("stock basic returned another symbol")
        listing_date = _parse_date(
            _required(row, "ipoDate", item.symbol),
            f"{item.symbol} ipoDate",
        )
        out_text = str(row.get("outDate", "")).strip()
        output.append(
            Qe5CapturedInstrument(
                symbol=item.symbol,
                board=item.board,
                listing_date=listing_date,
                delisting_date=(
                    _parse_date(out_text, f"{item.symbol} outDate")
                    if out_text
                    else None
                ),
            )
        )
    return tuple(output)


def _open_calendar(
    provider: Any,
    *,
    start_date: date,
    end_date: date,
) -> tuple[date, ...]:
    rows = _rows(
        provider.query_trade_dates(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        ),
        label="query_trade_dates",
    )
    values: list[date] = []
    prior: date | None = None
    for row in rows:
        current = _parse_date(
            _required(row, "calendar_date", "trade calendar"),
            "calendar_date",
        )
        if prior is not None and current <= prior:
            raise BaoStockCaptureError("trade calendar is not strictly ordered")
        prior = current
        is_open = _required(row, "is_trading_day", "trade calendar")
        if is_open not in {"0", "1"}:
            raise BaoStockCaptureError("trade calendar contains an invalid flag")
        if is_open == "1":
            values.append(current)
    if not values:
        raise BaoStockCaptureError("trade calendar contains no open dates")
    return tuple(values)


def _capture_bars(
    provider: Any,
    *,
    instruments: tuple[Qe5CapturedInstrument, ...],
    full_calendar: tuple[date, ...],
    start_date: date,
    end_date: date,
) -> tuple[Qe5CapturedDailyBar, ...]:
    requested_calendar = tuple(
        value for value in full_calendar if start_date <= value <= end_date
    )
    output: list[Qe5CapturedDailyBar] = []
    for instrument in instruments:
        code = _provider_code(instrument.symbol)
        raw_rows = _rows(
            provider.query_history_k_data_plus(
                code,
                ",".join(_HISTORY_FIELDS),
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency="d",
                adjustflag="3",
            ),
            label=f"query_history_raw:{instrument.symbol}",
        )
        qfq_rows = _rows(
            provider.query_history_k_data_plus(
                code,
                ",".join(_HISTORY_FIELDS),
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency="d",
                adjustflag="2",
            ),
            label=f"query_history_qfq:{instrument.symbol}",
        )
        raw = _history_by_date(raw_rows, symbol=instrument.symbol, adjustflag="3")
        qfq = _history_by_date(qfq_rows, symbol=instrument.symbol, adjustflag="2")
        if tuple(raw) != requested_calendar or tuple(qfq) != requested_calendar:
            raise BaoStockCaptureError(
                f"{instrument.symbol} history does not cover every requested open date"
            )
        for trade_date in requested_calendar:
            raw_row = raw[trade_date]
            qfq_row = qfq[trade_date]
            if (
                raw_row["tradestatus"] != qfq_row["tradestatus"]
                or raw_row["isST"] != qfq_row["isST"]
            ):
                raise BaoStockCaptureError(
                    f"{instrument.symbol} raw/qfq status mismatch on {trade_date}"
                )
            status = "traded" if raw_row["tradestatus"] == "1" else "suspended"
            output.append(
                Qe5CapturedDailyBar(
                    trade_date=trade_date,
                    known_at=datetime.combine(
                        trade_date,
                        time(15, 1),
                        tzinfo=SHANGHAI,
                    ),
                    symbol=instrument.symbol,
                    open_fen=_exact_fen(
                        _required(raw_row, "open", instrument.symbol),
                        f"{instrument.symbol} raw open {trade_date}",
                    ),
                    high_fen=_exact_fen(
                        _required(raw_row, "high", instrument.symbol),
                        f"{instrument.symbol} raw high {trade_date}",
                    ),
                    low_fen=_exact_fen(
                        _required(raw_row, "low", instrument.symbol),
                        f"{instrument.symbol} raw low {trade_date}",
                    ),
                    close_fen=_exact_fen(
                        _required(raw_row, "close", instrument.symbol),
                        f"{instrument.symbol} raw close {trade_date}",
                    ),
                    signal_close_fen=_rounded_fen(
                        _required(qfq_row, "close", instrument.symbol),
                        f"{instrument.symbol} qfq close {trade_date}",
                    ),
                    limit_reference_fen=_exact_fen(
                        _required(raw_row, "preclose", instrument.symbol),
                        f"{instrument.symbol} raw preclose {trade_date}",
                    ),
                    volume_shares=_integer(
                        str(raw_row.get("volume", "")),
                        f"{instrument.symbol} raw volume {trade_date}",
                        blank_zero=status == "suspended",
                    ),
                    amount_fen=_rounded_fen(
                        str(raw_row.get("amount", "")),
                        f"{instrument.symbol} raw amount {trade_date}",
                        blank_zero=status == "suspended",
                    ),
                    status=status,
                    is_st=raw_row["isST"] == "1",
                    listing_trade_day_number=bisect_right(
                        full_calendar,
                        trade_date,
                    )
                    - bisect_right(full_calendar, instrument.listing_date)
                    + int(instrument.listing_date in full_calendar),
                )
            )
    return tuple(sorted(output, key=lambda item: (item.trade_date, item.symbol)))


def _first_date(row: Mapping[str, str], names: tuple[str, ...], label: str) -> date:
    for name in names:
        text = str(row.get(name, "")).strip()
        if text:
            return _parse_date(text, f"{label} {name}")
    raise BaoStockCaptureError(f"{label} has no usable announcement date")


def _capture_actions(
    provider: Any,
    *,
    instruments: tuple[Qe5CapturedInstrument, ...],
    open_dates: set[date],
    start_date: date,
    end_date: date,
) -> tuple[Qe5CapturedCorporateAction, ...]:
    output: list[Qe5CapturedCorporateAction] = []
    seen_rows: set[tuple[str, ...]] = set()
    for instrument in instruments:
        for year in range(start_date.year, end_date.year + 1):
            rows = _rows(
                provider.query_dividend_data(
                    _provider_code(instrument.symbol),
                    year=year,
                    yearType="operate",
                ),
                label=f"query_dividend_data:{instrument.symbol}:{year}",
            )
            for row in rows:
                signature = tuple(f"{key}={row[key]}" for key in sorted(row))
                if signature in seen_rows:
                    continue
                seen_rows.add(signature)
                ex_text = str(row.get("dividOperateDate", "")).strip()
                if not ex_text:
                    continue
                ex_date = _parse_date(
                    ex_text,
                    f"{instrument.symbol} dividOperateDate",
                )
                if not start_date <= ex_date <= end_date:
                    continue
                label = f"{instrument.symbol} dividend {ex_date}"
                record_date = _parse_date(
                    _required(row, "dividRegistDate", label),
                    f"{label} record date",
                )
                known_date = _first_date(
                    row,
                    (
                        "dividPlanAnnounceDate",
                        "dividAgmPumDate",
                        "dividPreNoticeDate",
                    ),
                    label,
                )
                known_at = datetime.combine(
                    known_date,
                    time(0, 0),
                    tzinfo=SHANGHAI,
                )
                cash = _decimal(
                    str(row.get("dividCashPsBeforeTax", "")),
                    f"{label} cash before tax",
                    blank_zero=True,
                )
                stock = _decimal(
                    str(row.get("dividStocksPs", "")),
                    f"{label} stock per share",
                    blank_zero=True,
                ) + _decimal(
                    str(row.get("dividReserveToStockPs", "")),
                    f"{label} reserve stock per share",
                    blank_zero=True,
                )
                if cash < 0 or stock < 0:
                    raise BaoStockCaptureError(f"{label} contains a negative distribution")
                if cash == 0 and stock == 0:
                    continue
                if cash > 0:
                    pay_date = _parse_date(
                        _required(row, "dividPayDate", label),
                        f"{label} pay date",
                    )
                    cash_rate = Fraction(cash * 100)
                    output.append(
                        Qe5CapturedCorporateAction(
                            action_id=(
                                f"baostock.{instrument.symbol}.{ex_date}.cash."
                                f"{hashlib.sha256('|'.join(signature).encode()).hexdigest()[:12]}"
                            ),
                            symbol=instrument.symbol,
                            kind="cash_dividend",
                            known_at=known_at,
                            record_date=record_date,
                            ex_date=ex_date,
                            pay_date=pay_date,
                            multiplier_numerator=1,
                            multiplier_denominator=1,
                            cash_per_share_numerator_fen=cash_rate.numerator,
                            cash_per_share_denominator=cash_rate.denominator,
                            cash_rounding="half_up_total_fen",
                        )
                    )
                if stock > 0:
                    market_text = str(row.get("dividStockMarketDate", "")).strip()
                    market_date = (
                        _parse_date(market_text, f"{label} stock market date")
                        if market_text
                        else ex_date
                    )
                    multiplier = Fraction(Decimal(1) + stock)
                    output.append(
                        Qe5CapturedCorporateAction(
                            action_id=(
                                f"baostock.{instrument.symbol}.{ex_date}.shares."
                                f"{hashlib.sha256('|'.join(signature).encode()).hexdigest()[:12]}"
                            ),
                            symbol=instrument.symbol,
                            kind="share_split",
                            known_at=known_at,
                            record_date=record_date,
                            ex_date=ex_date,
                            pay_date=market_date,
                            multiplier_numerator=multiplier.numerator,
                            multiplier_denominator=multiplier.denominator,
                            cash_per_share_numerator_fen=0,
                            cash_per_share_denominator=1,
                            cash_rounding="none",
                        )
                    )
    output.sort(key=lambda item: (item.ex_date, item.action_id))
    for action in output:
        if (
            action.record_date not in open_dates
            or action.ex_date not in open_dates
            or action.pay_date not in open_dates
        ):
            raise BaoStockCaptureError(
                f"{action.action_id} crosses the capture boundary or uses a "
                "non-trading date; extend/review the source window"
            )
    return tuple(output)


def capture_baostock_market(
    manifest: Qe5UniverseManifest,
    *,
    start_date: date,
    end_date: date,
    as_of: date,
    provider_module: Any | None = None,
    captured_at: datetime | None = None,
) -> Qe5MarketCapture:
    """Capture one strict daily market bundle from BaoStock.

    Only explicitly classified, long-listed Shanghai/Shenzhen main-board
    instruments are accepted.  The adapter never infers a board from a code.
    """

    if start_date < TRANSITION_START_DATE:
        raise BaoStockCaptureError(
            f"BaoStock transition window must start on/after {TRANSITION_START_DATE}"
        )
    if not start_date < end_date <= as_of:
        raise BaoStockCaptureError("dates must satisfy start < end <= as_of")
    now = captured_at or datetime.now(SHANGHAI)
    if now.tzinfo is None or as_of > now.astimezone(SHANGHAI).date():
        raise BaoStockCaptureError("as_of cannot exceed the Shanghai capture date")

    if provider_module is None:
        try:
            import baostock as provider
        except ImportError as exc:
            raise BaoStockCaptureError("BaoStock is not installed") from exc
    else:
        provider = provider_module

    login = provider.login()
    if getattr(login, "error_code", None) != "0":
        raise BaoStockCaptureError(
            f"BaoStock login failed: {getattr(login, 'error_msg', 'unknown error')}"
        )
    try:
        instruments = _listing_metadata(provider, manifest)
        earliest_listing = min(item.listing_date for item in instruments)
        full_calendar = _open_calendar(
            provider,
            start_date=earliest_listing,
            end_date=end_date,
        )
        requested_calendar = tuple(
            value for value in full_calendar if start_date <= value <= end_date
        )
        bars = _capture_bars(
            provider,
            instruments=instruments,
            full_calendar=full_calendar,
            start_date=start_date,
            end_date=end_date,
        )
        actions = _capture_actions(
            provider,
            instruments=instruments,
            open_dates=set(requested_calendar),
            start_date=start_date,
            end_date=end_date,
        )
    finally:
        provider.logout()

    provider_version = str(getattr(provider, "__version__", "")).strip()
    if not provider_version:
        raise BaoStockCaptureError("BaoStock did not expose a provider version")
    anomalies = [
        "transitional_source:baostock",
        "baostock_current_consolidated_history:no_revision_archive",
        "qfq_signal_price:rounded_half_up_to_fen",
        "raw_amount:rounded_half_up_to_fen",
        "cash_dividend_total:rounded_half_up_to_fen",
        f"explicit_board_mapping:{manifest.content_sha256}",
    ]
    if any(item.kind == "cash_dividend" for item in actions):
        anomalies.append(
            "cash_dividend_before_tax:differentiated_holding_period_tax_not_modeled"
        )
    return Qe5MarketCapture(
        provider="baostock",
        provider_version=provider_version,
        universe_id=manifest.universe_id,
        universe_manifest_sha256=manifest.content_sha256,
        captured_at=now,
        as_of=as_of,
        start_date=start_date,
        end_date=end_date,
        instruments=instruments,
        calendar=requested_calendar,
        bars=bars,
        corporate_actions=actions,
        anomalies=tuple(anomalies),
    )
