"""Independent QE6-2 ordinary-trade replay through vn.py's EventEngine.

The replay deliberately excludes fees and A-share special rules.  It consumes
only chronological open-market order intents and daily marks, then lets vn.py
order/trade events drive an independent integer-fen account state.  QE6-3 owns
fees, T+1, board-lot normalization, price limits, suspensions, and corporate
actions.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import re
from threading import Event as ThreadEvent
from typing import Any, Callable, Mapping

from formal_event_path import parse_qe5_identity_payload
from worker_runtime import WorkerError, canonical_json


ORDINARY_REPLAY_REQUEST_SCHEMA = "vibe.vnpy-ordinary-replay-request.v1"
ORDINARY_REPLAY_RESULT_SCHEMA = "vibe.vnpy-ordinary-replay-result.v1"

_PAYLOAD_KEYS = {
    "schema_version",
    "identity",
    "qe5_backtest_input_sha256",
    "qe5_ledger_sha256",
    "initial_cash_fen",
    "events",
}
_EVENT_KEYS = {
    "source_sequence",
    "event",
    "trade_date",
    "order",
    "mark_prices_fen",
}
_ORDER_KEYS = {
    "order_id",
    "symbol",
    "side",
    "requested_shares",
    "price_fen",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            f"{label} keys do not match schema",
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be positive")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            f"{label} must be a non-negative integer",
        )
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            f"{label} must be lowercase SHA-256",
        )
    return value


def _trade_date(value: Any) -> date:
    if not isinstance(value, str):
        raise WorkerError("INVALID_OPERATION_INPUT", "trade_date must be ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "trade_date must be ISO date",
        ) from exc


def _parse_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "ordinary replay events must be a non-empty list",
        )
    parsed: list[dict[str, Any]] = []
    order_ids: set[str] = set()
    previous_date: date | None = None
    previous_sequence = -1
    mark_dates: set[date] = set()
    for raw in value:
        item = _exact(raw, _EVENT_KEYS, "ordinary replay event")
        source_sequence = _non_negative_int(
            item["source_sequence"],
            "source_sequence",
        )
        if source_sequence <= previous_sequence:
            raise WorkerError(
                "INVALID_OPERATION_INPUT",
                "source_sequence must be strictly increasing",
            )
        current_date = _trade_date(item["trade_date"])
        if previous_date is not None and current_date < previous_date:
            raise WorkerError(
                "INVALID_OPERATION_INPUT",
                "ordinary replay events must be chronological",
            )
        event_kind = item["event"]
        if event_kind == "order":
            order = _exact(item["order"], _ORDER_KEYS, "ordinary order")
            if item["mark_prices_fen"] is not None:
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "ordinary order cannot contain daily marks",
                )
            order_id = order["order_id"]
            symbol = order["symbol"]
            side = order["side"]
            if (
                not isinstance(order_id, str)
                or _ORDER_ID_RE.fullmatch(order_id) is None
                or order_id in order_ids
            ):
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "ordinary order_id is invalid or duplicated",
                )
            if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "ordinary order symbol is invalid",
                )
            if side not in {"buy", "sell"}:
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "ordinary order side is invalid",
                )
            parsed_order = {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "requested_shares": _positive_int(
                    order["requested_shares"],
                    "requested_shares",
                ),
                "price_fen": _positive_int(order["price_fen"], "price_fen"),
            }
            order_ids.add(order_id)
            parsed.append(
                {
                    "source_sequence": source_sequence,
                    "event": "order",
                    "trade_date": current_date,
                    "order": parsed_order,
                    "mark_prices_fen": None,
                }
            )
        elif event_kind == "mark":
            if item["order"] is not None or not isinstance(
                item["mark_prices_fen"], Mapping
            ):
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "daily mark has an invalid shape",
                )
            if current_date in mark_dates:
                raise WorkerError(
                    "INVALID_OPERATION_INPUT",
                    "daily marks must be unique by date",
                )
            marks: dict[str, int] = {}
            for symbol, price in item["mark_prices_fen"].items():
                if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
                    raise WorkerError(
                        "INVALID_OPERATION_INPUT",
                        "daily mark symbol is invalid",
                    )
                marks[symbol] = _positive_int(price, "mark price")
            parsed.append(
                {
                    "source_sequence": source_sequence,
                    "event": "mark",
                    "trade_date": current_date,
                    "order": None,
                    "mark_prices_fen": marks,
                }
            )
            mark_dates.add(current_date)
        else:
            raise WorkerError(
                "UNSUPPORTED_QE6_2_EVENT",
                "QE6-2 only supports ordinary order and mark events",
            )
        previous_date = current_date
        previous_sequence = source_sequence
    if not mark_dates:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "ordinary replay requires at least one daily mark",
        )
    return parsed


def _parse_payload(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    request = _exact(payload, _PAYLOAD_KEYS, "ordinary replay payload")
    if request["schema_version"] != ORDINARY_REPLAY_REQUEST_SCHEMA:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "unsupported ordinary replay request schema",
        )
    identity = parse_qe5_identity_payload(request["identity"], snapshot=snapshot)
    initial_cash_fen = _non_negative_int(
        request["initial_cash_fen"],
        "initial_cash_fen",
    )
    events = _parse_events(request["events"])
    return {
        **identity,
        "qe5_backtest_input_sha256": _sha256(
            request["qe5_backtest_input_sha256"],
            "qe5_backtest_input_sha256",
        ),
        "qe5_ledger_sha256": _sha256(
            request["qe5_ledger_sha256"],
            "qe5_ledger_sha256",
        ),
        "initial_cash_fen": initial_cash_fen,
        "events": events,
        "ordinary_replay_input_sha256": _canonical_sha256(request),
    }


def _ordinary_replay(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any]:
    parsed = _parse_payload(payload, snapshot)
    try:
        boundary = load_boundary()
        event_type = boundary["Event"]
        event_engine_type = boundary["EventEngine"]
        direction_type = boundary["Direction"]
        exchange_type = boundary["Exchange"]
        status_type = boundary["Status"]
        order_type = boundary["OrderData"]
        trade_type = boundary["TradeData"]
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(
            "ENGINE_IMPORT_ERROR",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    cash_fen = parsed["initial_cash_fen"]
    positions: dict[str, int] = {}
    order_inputs: dict[str, dict[str, Any]] = {}
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    daily_accounts: list[dict[str, Any]] = []
    event_receipts: list[dict[str, Any]] = []
    semantic_errors: list[str] = []
    completed = ThreadEvent()

    def acknowledge() -> None:
        completed.set()

    def on_order(event: Any) -> None:
        order = event.data
        source = order_inputs[order.orderid]
        status = order.status.name
        reason = source.get("reason")
        expected_status = "REJECTED" if reason is not None else "ALLTRADED"
        expected_traded = 0 if reason is not None else source["requested_shares"]
        if (
            status != expected_status
            or int(order.volume) != source["requested_shares"]
            or int(order.traded) != expected_traded
            or round(float(order.price) * 100) != source["price_fen"]
        ):
            semantic_errors.append("vn.py OrderData changed an ordinary order")
            acknowledge()
            return
        normalized = {
            "source_sequence": source["source_sequence"],
            "order_id": order.orderid,
            "trade_date": source["trade_date"].isoformat(),
            "symbol": source["symbol"],
            "side": source["side"],
            "requested_shares": source["requested_shares"],
            "price_fen": source["price_fen"],
            "status": "rejected" if status == "REJECTED" else "filled",
            "reason": reason,
        }
        orders.append(normalized)
        event_receipts.append(
            {
                "kind": "order",
                "source_sequence": source["source_sequence"],
                "object_id": order.orderid,
            }
        )
        if status == "REJECTED":
            rejections.append(
                {
                    "source_sequence": source["source_sequence"],
                    "order_id": order.orderid,
                    "trade_date": source["trade_date"].isoformat(),
                    "symbol": source["symbol"],
                    "side": source["side"],
                    "requested_shares": source["requested_shares"],
                    "price_fen": source["price_fen"],
                    "reason": reason,
                    "cash_fen": cash_fen,
                    "positions": dict(sorted(positions.items())),
                }
            )
        acknowledge()

    def on_trade(event: Any) -> None:
        nonlocal cash_fen
        trade = event.data
        source = order_inputs[trade.orderid]
        shares = int(trade.volume)
        price_fen = source["price_fen"]
        if (
            shares != source["requested_shares"]
            or round(float(trade.price) * 100) != price_fen
        ):
            semantic_errors.append("vn.py TradeData changed an ordinary fill")
            acknowledge()
            return
        if source["side"] == "buy":
            cash_delta_fen = -(shares * price_fen)
            positions[source["symbol"]] = positions.get(source["symbol"], 0) + shares
        else:
            cash_delta_fen = shares * price_fen
            remaining = positions[source["symbol"]] - shares
            if remaining:
                positions[source["symbol"]] = remaining
            else:
                del positions[source["symbol"]]
        cash_fen += cash_delta_fen
        fills.append(
            {
                "source_sequence": source["source_sequence"],
                "order_id": trade.orderid,
                "trade_id": trade.tradeid,
                "trade_date": source["trade_date"].isoformat(),
                "symbol": source["symbol"],
                "side": source["side"],
                "filled_shares": shares,
                "price_fen": price_fen,
                "cash_delta_fen": cash_delta_fen,
                "cash_fen": cash_fen,
                "positions": dict(sorted(positions.items())),
            }
        )
        event_receipts.append(
            {
                "kind": "trade",
                "source_sequence": source["source_sequence"],
                "object_id": trade.tradeid,
            }
        )
        acknowledge()

    def on_mark(event: Any) -> None:
        data = event.data
        marks = data["mark_prices_fen"]
        if set(marks) != set(positions):
            event_receipts.append(
                {
                    "kind": "invalid_mark",
                    "source_sequence": data["source_sequence"],
                    "object_id": data["trade_date"],
                }
            )
            acknowledge()
            return
        market_value_fen = sum(
            positions[symbol] * price for symbol, price in marks.items()
        )
        daily_accounts.append(
            {
                "source_sequence": data["source_sequence"],
                "trade_date": data["trade_date"],
                "cash_fen": cash_fen,
                "positions": dict(sorted(positions.items())),
                "mark_prices_fen": dict(sorted(marks.items())),
                "market_value_fen": market_value_fen,
                "equity_fen": cash_fen + market_value_fen,
            }
        )
        event_receipts.append(
            {
                "kind": "mark",
                "source_sequence": data["source_sequence"],
                "object_id": data["trade_date"],
            }
        )
        acknowledge()

    engine = event_engine_type(interval=0.01)
    engine.register("eOrder", on_order)
    engine.register("eTrade", on_trade)
    engine.register("eVibeDailyMark", on_mark)
    engine.start()

    def dispatch(event: Any) -> None:
        completed.clear()
        engine.put(event)
        if not completed.wait(timeout=2):
            raise WorkerError(
                "ENGINE_SEMANTIC_ERROR",
                "vn.py EventEngine did not deliver an ordinary replay event",
            )
        if semantic_errors:
            raise WorkerError(
                "ENGINE_SEMANTIC_ERROR",
                semantic_errors[0],
            )

    try:
        for item in parsed["events"]:
            if item["event"] == "mark":
                dispatch(
                    event_type(
                        "eVibeDailyMark",
                        {
                            "source_sequence": item["source_sequence"],
                            "trade_date": item["trade_date"].isoformat(),
                            "mark_prices_fen": item["mark_prices_fen"],
                        },
                    )
                )
                if event_receipts[-1]["kind"] == "invalid_mark":
                    raise WorkerError(
                        "ORDINARY_REPLAY_DIVERGENCE",
                        "daily marks do not cover the vn.py oracle positions",
                    )
                continue

            source = {
                "source_sequence": item["source_sequence"],
                "trade_date": item["trade_date"],
                **item["order"],
            }
            order_id = source["order_id"]
            held = positions.get(source["symbol"], 0)
            if source["side"] == "buy" and (
                source["requested_shares"] * source["price_fen"] > cash_fen
            ):
                source["reason"] = "INSUFFICIENT_CASH"
            elif source["side"] == "sell" and held == 0:
                source["reason"] = "NO_POSITION"
            elif source["side"] == "sell" and source["requested_shares"] > held:
                source["reason"] = "INSUFFICIENT_POSITION"
            else:
                source["reason"] = None
            order_inputs[order_id] = source
            raw_symbol, suffix = source["symbol"].split(".")
            exchange = {
                "SH": exchange_type.SSE,
                "SZ": exchange_type.SZSE,
                "BJ": exchange_type.BSE,
            }[suffix]
            direction = (
                direction_type.LONG
                if source["side"] == "buy"
                else direction_type.SHORT
            )
            timestamp = datetime.combine(source["trade_date"], datetime.min.time())
            rejected = source["reason"] is not None
            dispatch(
                event_type(
                    "eOrder",
                    order_type(
                        gateway_name="QE6",
                        symbol=raw_symbol,
                        exchange=exchange,
                        orderid=order_id,
                        direction=direction,
                        price=source["price_fen"] / 100,
                        volume=source["requested_shares"],
                        traded=0 if rejected else source["requested_shares"],
                        status=(
                            status_type.REJECTED
                            if rejected
                            else status_type.ALLTRADED
                        ),
                        datetime=timestamp,
                    ),
                )
            )
            if rejected:
                continue
            dispatch(
                event_type(
                    "eTrade",
                    trade_type(
                        gateway_name="QE6",
                        symbol=raw_symbol,
                        exchange=exchange,
                        orderid=order_id,
                        tradeid=f"qe6-{order_id}",
                        direction=direction,
                        price=source["price_fen"] / 100,
                        volume=source["requested_shares"],
                        datetime=timestamp,
                    ),
                )
            )
    finally:
        engine.stop()

    return {
        "operation_schema": ORDINARY_REPLAY_RESULT_SCHEMA,
        "snapshot_sha256": parsed["snapshot_sha256"],
        "engine_request_id": parsed["engine_request"]["request_id"],
        "execution_plan_sha256": parsed["execution_plan_sha256"],
        "qe5_backtest_input_sha256": parsed["qe5_backtest_input_sha256"],
        "qe5_ledger_sha256": parsed["qe5_ledger_sha256"],
        "ordinary_replay_input_sha256": parsed["ordinary_replay_input_sha256"],
        "initial_cash_fen": parsed["initial_cash_fen"],
        "orders": orders,
        "fills": fills,
        "rejections": rejections,
        "daily_accounts": daily_accounts,
        "event_receipts": event_receipts,
        "engine_version": boundary["version"],
        "source_sha256": boundary["source_sha256"],
    }


def build_ordinary_replay_handler(
    load_boundary: Callable[[], Mapping[str, Any]],
) -> Callable[[Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]]:
    return lambda payload, snapshot: _ordinary_replay(
        payload,
        snapshot,
        load_boundary,
    )
