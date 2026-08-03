"""QE6-3 independent China-A rule replay inside vn.py's EventEngine."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
from threading import Event as ThreadEvent
from typing import Any, Callable, Mapping

from formal_event_path import parse_qe5_identity_payload
from worker_runtime import WorkerError, canonical_json


REQUEST_SCHEMA = "vibe.vnpy-china-a-replay-request.v1"
RESULT_SCHEMA = "vibe.vnpy-china-a-replay-result.v1"
_TOP_KEYS = {
    "schema_version", "identity", "qe5_backtest_input_sha256",
    "qe5_ledger_sha256", "ledger", "events",
}
_PROJECTION_KEYS = {
    "sequence", "trade_date", "event", "outcome", "reason", "order_id",
    "symbol", "side", "requested_shares", "filled_shares", "price_fen",
    "position_delta", "cash_delta_fen", "dividend_receivable_delta_fen",
    "fees", "positions", "sellable_positions", "cash_fen",
    "dividend_receivable_fen", "mark_prices_fen", "market_value_fen",
    "equity_fen",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} keys do not match schema")
    return value


def _half_up(notional: int, tenths_bps: int) -> int:
    quotient, remainder = divmod(notional * tenths_bps, 100_000)
    return quotient + int(remainder * 2 >= 100_000)


class _Account:
    def __init__(self, opening: Mapping[str, Any], rules: Mapping[str, Any], board_lot: int) -> None:
        self.cash = int(opening["cash_fen"])
        self.receivables: dict[str, int] = {}
        self.lots: dict[str, list[list[Any]]] = defaultdict(list)
        self.marks: dict[str, int] = {}
        self.rules = rules
        self.board_lot = board_lot

    def positions(self) -> dict[str, int]:
        return {symbol: sum(int(lot[1]) for lot in lots) for symbol, lots in self.lots.items() if lots}

    def sellable(self, day: date) -> dict[str, int]:
        return {
            symbol: sum(int(quantity) for acquired, quantity in lots if acquired < day)
            for symbol, lots in self.lots.items() if lots
        }

    def fees(self, shares: int, price: int, side: str) -> dict[str, int]:
        notional = shares * price
        commission = max(
            _half_up(notional, int(self.rules["commission_tenths_bps"])),
            int(self.rules["minimum_commission_fen"]),
        )
        transfer = _half_up(notional, int(self.rules["transfer_fee_tenths_bps"]))
        tax = _half_up(notional, int(self.rules["sell_tax_tenths_bps"])) if side == "sell" else 0
        return {
            "commission_fen": commission,
            "sell_tax_fen": tax,
            "transfer_fee_fen": transfer,
            "total_fen": commission + tax + transfer,
        }

    def projection(self, base: dict[str, Any], day: date) -> dict[str, Any]:
        positions = self.positions()
        sellable = self.sellable(day)
        if set(self.marks) != set(positions):
            raise WorkerError("CHINA_A_REPLAY_DIVERGENCE", "marks do not cover positions")
        market_value = sum(quantity * self.marks[symbol] for symbol, quantity in positions.items())
        return {
            **base,
            "positions": dict(sorted(positions.items())),
            "sellable_positions": dict(sorted(sellable.items())),
            "cash_fen": self.cash,
            "dividend_receivable_fen": sum(self.receivables.values()),
            "mark_prices_fen": dict(sorted(self.marks.items())),
            "market_value_fen": market_value,
            "equity_fen": self.cash + sum(self.receivables.values()) + market_value,
        }

    def order(self, sequence: int, event: Mapping[str, Any]) -> dict[str, Any]:
        raw = event["order"]
        day = date.fromisoformat(event["trade_date"])
        symbol, side = raw["symbol"], raw["side"]
        requested, price = int(raw["requested_shares"]), int(raw["price_fen"])
        held = self.positions().get(symbol, 0)
        reason = None
        filled = 0
        if raw["market_state"] == "suspended":
            reason = "SUSPENDED"
        elif raw["market_state"] == "delisted":
            reason = "DELISTED"
        elif raw["market_state"] == "locked_limit_up" and side == "buy":
            reason = "LIMIT_UP_LOCKED"
        elif raw["market_state"] == "locked_limit_down" and side == "sell":
            reason = "LIMIT_DOWN_LOCKED"
        elif side == "buy":
            normalized = requested // self.board_lot * self.board_lot
            capacity = normalized if raw["maximum_fill_shares"] is None else int(raw["maximum_fill_shares"]) // self.board_lot * self.board_lot
            if normalized == 0:
                reason = "INVALID_BOARD_LOT"
            elif capacity == 0:
                reason = "NO_LIQUIDITY"
            else:
                filled = min(normalized, capacity)
                fees = self.fees(filled, price, side)
                if self.cash < filled * price + fees["total_fen"]:
                    reason, filled = "INSUFFICIENT_CASH", 0
                else:
                    self.cash -= filled * price + fees["total_fen"]
                    self.lots[symbol].append([day, filled])
                    reason = "CAPACITY_LIMITED" if filled < normalized else ("BOARD_LOT_ROUNDED" if normalized < requested else None)
        else:
            sellable = self.sellable(day).get(symbol, 0)
            if held == 0:
                reason = "NO_POSITION"
            elif requested > held:
                reason = "INSUFFICIENT_POSITION"
            elif sellable == 0:
                reason = "T1_LOCKED"
            elif requested > sellable:
                reason = "INSUFFICIENT_SELLABLE"
            else:
                capacity = requested if raw["maximum_fill_shares"] is None else int(raw["maximum_fill_shares"])
                if capacity == 0:
                    reason = "NO_LIQUIDITY"
                else:
                    filled = min(requested, capacity)
                    fees = self.fees(filled, price, side)
                    remaining = filled
                    next_lots = []
                    for acquired, quantity in self.lots[symbol]:
                        take = min(quantity, remaining) if acquired < day else 0
                        quantity -= take
                        remaining -= take
                        if quantity:
                            next_lots.append([acquired, quantity])
                    if next_lots:
                        self.lots[symbol] = next_lots
                    else:
                        del self.lots[symbol]
                    self.cash += filled * price - fees["total_fen"]
                    reason = "CAPACITY_LIMITED" if filled < requested else None
        rejected = filled == 0
        fees = {"commission_fen": 0, "sell_tax_fen": 0, "transfer_fee_fen": 0, "total_fen": 0} if rejected else self.fees(filled, price, side)
        if symbol in self.positions():
            self.marks[symbol] = price
        else:
            self.marks.pop(symbol, None)
        delta = {} if rejected else {symbol: filled if side == "buy" else -filled}
        cash_delta = 0 if rejected else (-(filled * price + fees["total_fen"]) if side == "buy" else filled * price - fees["total_fen"])
        return self.projection({
            "sequence": sequence, "trade_date": day.isoformat(), "event": "rejected_order" if rejected else side,
            "outcome": "rejected" if rejected else ("partially_filled" if reason else "filled"), "reason": reason,
            "order_id": raw["order_id"], "symbol": symbol, "side": side, "requested_shares": requested,
            "filled_shares": filled, "price_fen": price, "position_delta": delta, "cash_delta_fen": cash_delta,
            "dividend_receivable_delta_fen": 0, "fees": fees,
        }, day)

    def action(self, sequence: int, event: Mapping[str, Any]) -> dict[str, Any]:
        kind = event["event"]
        day = date.fromisoformat(event["trade_date"])
        symbol = event["symbol"]
        self.marks = dict(event["mark_prices_fen"])
        delta: dict[str, int] = {}
        cash_delta = 0
        recv_delta = 0
        if kind == "share_split":
            before = self.positions()[symbol]
            new = []
            for acquired, quantity in self.lots[symbol]:
                numerator = quantity * int(event["multiplier_numerator"])
                value, remainder = divmod(numerator, int(event["multiplier_denominator"]))
                if remainder:
                    raise WorkerError("CHINA_A_REPLAY_DIVERGENCE", "fractional split")
                new.append([acquired, value])
            self.lots[symbol] = new
            delta = {symbol: self.positions()[symbol] - before}
        elif kind == "dividend_ex":
            numerator = event["cash_per_share_fen"] or event["cash_per_share_numerator_fen"]
            denominator = 1 if event["cash_per_share_fen"] else event["cash_per_share_denominator"]
            recv_delta, remainder = divmod(int(event["entitled_shares"]) * int(numerator), int(denominator))
            if remainder:
                if event["cash_rounding"] != "half_up_total_fen":
                    raise WorkerError("CHINA_A_REPLAY_DIVERGENCE", "fractional dividend")
                recv_delta += int(remainder * 2 >= int(denominator))
            self.receivables[symbol] = self.receivables.get(symbol, 0) + recv_delta
        else:
            cash_delta = self.receivables.pop(symbol, 0)
            recv_delta = -cash_delta
            self.cash += cash_delta
        return self.projection({
            "sequence": sequence, "trade_date": day.isoformat(), "event": kind, "outcome": "applied", "reason": None,
            "order_id": None, "symbol": symbol, "side": None, "requested_shares": 0, "filled_shares": 0,
            "price_fen": self.marks[symbol], "position_delta": delta, "cash_delta_fen": cash_delta,
            "dividend_receivable_delta_fen": recv_delta,
            "fees": {"commission_fen": 0, "sell_tax_fen": 0, "transfer_fee_fen": 0, "total_fen": 0},
        }, day)

    def mark(self, sequence: int, event: Mapping[str, Any]) -> dict[str, Any]:
        day = date.fromisoformat(event["trade_date"])
        self.marks = dict(event["mark_prices_fen"])
        return self.projection({
            "sequence": sequence, "trade_date": day.isoformat(), "event": "mark", "outcome": "applied", "reason": None,
            "order_id": None, "symbol": None, "side": None, "requested_shares": 0, "filled_shares": 0, "price_fen": None,
            "position_delta": {}, "cash_delta_fen": 0, "dividend_receivable_delta_fen": 0,
            "fees": {"commission_fen": 0, "sell_tax_fen": 0, "transfer_fee_fen": 0, "total_fen": 0},
        }, day)


def build_china_a_replay_handler(load_boundary: Callable[[], Mapping[str, Any]]) -> Callable:
    def replay(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
        request = _exact(payload, _TOP_KEYS, "China-A replay payload")
        if request["schema_version"] != REQUEST_SCHEMA:
            raise WorkerError("INVALID_OPERATION_INPUT", "unsupported China-A replay schema")
        identity = parse_qe5_identity_payload(request["identity"], snapshot=snapshot)
        ledger = request["ledger"]
        events = request["events"]
        entries = ledger["entries"]
        if len(entries) != len(events) + 1:
            raise WorkerError("INVALID_OPERATION_INPUT", "ledger/event count mismatch")
        boundary = load_boundary()
        Event, EventEngine = boundary["Event"], boundary["EventEngine"]
        account = _Account(entries[0], ledger["rules"], int(ledger["board_lot"]))
        receipts = []
        completed = ThreadEvent()
        failure: list[str] = []

        def handler(event: Any) -> None:
            index, raw = event.data["index"], event.data["event"]
            try:
                if raw["event"] == "order":
                    actual = account.order(index + 1, raw)
                elif raw["event"] == "mark":
                    actual = account.mark(index + 1, raw)
                else:
                    actual = account.action(index + 1, raw)
                expected = {key: entries[index + 1][key] for key in _PROJECTION_KEYS}
                if canonical_json(actual) != canonical_json(expected):
                    failure.append(f"sequence {index + 1}")
                else:
                    receipts.append({"sequence": index + 1, "event": actual["event"]})
            except Exception as exc:
                failure.append(f"sequence {index + 1}: {type(exc).__name__}: {exc}")
            finally:
                completed.set()

        engine = EventEngine(interval=0.01)
        engine.register("eVibeChinaAReplay", handler)
        engine.start()
        try:
            for index, raw in enumerate(events):
                completed.clear()
                engine.put(Event("eVibeChinaAReplay", {"index": index, "event": raw}))
                if not completed.wait(2):
                    raise WorkerError("ENGINE_SEMANTIC_ERROR", "vn.py did not deliver China-A event")
                if failure:
                    raise WorkerError("CHINA_A_REPLAY_DIVERGENCE", failure[0])
        finally:
            engine.stop()
        return {
            "operation_schema": RESULT_SCHEMA, "snapshot_sha256": identity["snapshot_sha256"],
            "engine_request_id": identity["engine_request"]["request_id"],
            "execution_plan_sha256": identity["execution_plan_sha256"],
            "qe5_backtest_input_sha256": request["qe5_backtest_input_sha256"], "qe5_ledger_sha256": request["qe5_ledger_sha256"],
            "china_a_replay_input_sha256": _sha(request), "reconciled_entries": len(receipts), "receipts": receipts,
            "engine_version": boundary["version"], "source_sha256": boundary["source_sha256"],
        }
    return replay
