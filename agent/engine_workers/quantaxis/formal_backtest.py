"""Strict QE5 QUANTAXIS strategy backtest over a content-bound snapshot.

The worker owns signal evaluation and order generation.  Its emitted events are
replayed by Vibe's engine-neutral integer-fen oracle, so QUANTAXIS never becomes
the persisted accounting contract.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from worker_runtime import WorkerError, canonical_json


BACKTEST_SNAPSHOT_SCHEMA = "vibe.quantaxis-backtest-snapshot.v1"
BACKTEST_REQUEST_SCHEMA = "vibe.quantaxis-backtest-request.v1"
BACKTEST_RESULT_SCHEMA = "vibe.quantaxis-backtest-result.v1"
RULE_TABLE_SCHEMA = "vibe.cn-equity-rule-table.v1"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} keys do not match schema")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} is below its minimum")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be finite")
    return result


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} is invalid") from exc
    return value


def _token(value: Any, label: str, pattern: re.Pattern[str] = _ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} is invalid")
    return value


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _load_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        raise WorkerError("SNAPSHOT_REQUIRED", "backtest requires a content-bound snapshot")
    path = Path(str(snapshot["path"]))
    if not path.is_file():
        raise WorkerError("INVALID_OPERATION_INPUT", "backtest snapshot must be one JSON file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError("INVALID_OPERATION_INPUT", "backtest snapshot is invalid JSON") from exc
    root = _exact(
        raw,
        {
            "schema_version",
            "price_semantics",
            "rule_table",
            "instruments",
            "calendar",
            "bars",
            "corporate_actions",
        },
        "backtest snapshot",
    )
    if root["schema_version"] != BACKTEST_SNAPSHOT_SCHEMA:
        raise WorkerError("INVALID_OPERATION_INPUT", "unsupported backtest snapshot schema")
    return {**root, "snapshot_sha256": str(snapshot["sha256"])}


def _parse_price_semantics(value: Any) -> dict[str, str]:
    semantics = _exact(
        value,
        {
            "execution_price_adjustment",
            "signal_price_adjustment",
            "corporate_action_mode",
        },
        "price_semantics",
    )
    if semantics["execution_price_adjustment"] != "raw":
        raise WorkerError(
            "UNSUPPORTED_SEMANTICS",
            "QE5-2 execution and accounting prices must be raw",
        )
    if semantics["signal_price_adjustment"] not in {"raw", "qfq", "hfq"}:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "signal price adjustment is invalid",
        )
    if semantics["corporate_action_mode"] != "explicit":
        raise WorkerError(
            "UNSUPPORTED_SEMANTICS",
            "raw execution requires explicit corporate actions",
        )
    return dict(semantics)


def _parse_rule_table(value: Any) -> dict[str, Any]:
    table = _exact(
        value,
        {
            "schema_version",
            "version",
            "fee_schedule",
            "market_rules",
            "max_participation_bps",
        },
        "rule_table",
    )
    if table["schema_version"] != RULE_TABLE_SCHEMA:
        raise WorkerError("INVALID_OPERATION_INPUT", "unsupported rule table schema")
    version = _token(table["version"], "rule_table.version")
    participation = _integer(
        table["max_participation_bps"],
        "rule_table.max_participation_bps",
        minimum=1,
    )
    if participation > 10_000:
        raise WorkerError("INVALID_OPERATION_INPUT", "participation bps exceeds 10000")
    fee = _exact(
        table["fee_schedule"],
        {
            "effective_from",
            "effective_to",
            "commission_tenths_bps",
            "minimum_commission_fen",
            "sell_tax_tenths_bps",
            "transfer_fee_tenths_bps",
            "rule_version",
        },
        "fee_schedule",
    )
    normalized_fee = {
        "effective_from": _date(fee["effective_from"], "fee effective_from"),
        "effective_to": _date(fee["effective_to"], "fee effective_to"),
        **{
            key: _integer(fee[key], f"fee_schedule.{key}", minimum=0)
            for key in (
                "commission_tenths_bps",
                "minimum_commission_fen",
                "sell_tax_tenths_bps",
                "transfer_fee_tenths_bps",
            )
        },
        "rule_version": _token(fee["rule_version"], "fee_schedule.rule_version"),
    }
    if normalized_fee["effective_from"] > normalized_fee["effective_to"]:
        raise WorkerError("INVALID_OPERATION_INPUT", "fee schedule dates are reversed")

    market_rules: dict[str, dict[str, Any]] = {}
    if not isinstance(table["market_rules"], list) or not table["market_rules"]:
        raise WorkerError("INVALID_OPERATION_INPUT", "market_rules must be non-empty")
    keys = {
        "rule_id",
        "effective_from",
        "effective_to",
        "board",
        "is_st",
        "listing_day_min",
        "listing_day_max",
        "limit_up_bps",
        "limit_down_bps",
        "tick_fen",
    }
    for index, item in enumerate(table["market_rules"]):
        rule = _exact(item, keys, f"market_rules[{index}]")
        rule_id = _token(rule["rule_id"], f"market_rules[{index}].rule_id")
        if rule_id in market_rules:
            raise WorkerError("INVALID_OPERATION_INPUT", "duplicate market rule id")
        normalized = {
            "rule_id": rule_id,
            "effective_from": _date(rule["effective_from"], "market rule effective_from"),
            "effective_to": _date(rule["effective_to"], "market rule effective_to"),
            "board": _token(rule["board"], "market rule board"),
            "is_st": rule["is_st"],
            "listing_day_min": _integer(rule["listing_day_min"], "listing_day_min", minimum=0),
            "listing_day_max": (
                None
                if rule["listing_day_max"] is None
                else _integer(rule["listing_day_max"], "listing_day_max", minimum=0)
            ),
            "limit_up_bps": (
                None
                if rule["limit_up_bps"] is None
                else _integer(rule["limit_up_bps"], "limit_up_bps", minimum=0)
            ),
            "limit_down_bps": (
                None
                if rule["limit_down_bps"] is None
                else _integer(rule["limit_down_bps"], "limit_down_bps", minimum=0)
            ),
            "tick_fen": _integer(rule["tick_fen"], "tick_fen", minimum=1),
        }
        if not isinstance(normalized["is_st"], bool):
            raise WorkerError("INVALID_OPERATION_INPUT", "market rule is_st must be boolean")
        if normalized["effective_from"] > normalized["effective_to"]:
            raise WorkerError("INVALID_OPERATION_INPUT", "market rule dates are reversed")
        if (normalized["limit_up_bps"] is None) != (
            normalized["limit_down_bps"] is None
        ):
            raise WorkerError("INVALID_OPERATION_INPUT", "price limits must be both present or absent")
        market_rules[rule_id] = normalized
    return {
        "version": version,
        "fee_schedule": normalized_fee,
        "market_rules": market_rules,
        "max_participation_bps": participation,
    }


def _parse_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    root = _load_snapshot(snapshot)
    price_semantics = _parse_price_semantics(root["price_semantics"])
    rules = _parse_rule_table(root["rule_table"])
    instruments: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(root["instruments"]):
        instrument = _exact(
            item,
            {"symbol", "board", "listing_date", "delisting_date"},
            f"instruments[{index}]",
        )
        symbol = _token(instrument["symbol"], "instrument symbol", _SYMBOL_RE)
        if symbol in instruments:
            raise WorkerError("INVALID_OPERATION_INPUT", "duplicate instrument")
        instruments[symbol] = {
            "symbol": symbol,
            "board": _token(instrument["board"], "instrument board"),
            "listing_date": _date(instrument["listing_date"], "listing_date"),
            "delisting_date": (
                None
                if instrument["delisting_date"] is None
                else _date(instrument["delisting_date"], "delisting_date")
            ),
        }

    calendar: list[str] = []
    for index, item in enumerate(root["calendar"]):
        day = _exact(item, {"trade_date", "is_open"}, f"calendar[{index}]")
        trade_date = _date(day["trade_date"], "calendar trade_date")
        if day["is_open"] is True:
            calendar.append(trade_date)
        elif day["is_open"] is not False:
            raise WorkerError("INVALID_OPERATION_INPUT", "calendar is_open must be boolean")
    if calendar != sorted(calendar) or len(calendar) != len(set(calendar)):
        raise WorkerError("INVALID_OPERATION_INPUT", "open calendar dates must be unique and sorted")

    bar_keys = {
        "trade_date",
        "known_at",
        "symbol",
        "open_fen",
        "high_fen",
        "low_fen",
        "close_fen",
        "signal_close_fen",
        "limit_reference_fen",
        "volume_shares",
        "status",
        "is_st",
        "listing_trade_day_number",
        "market_rule_id",
        "features",
    }
    bars: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(root["bars"]):
        bar = _exact(item, bar_keys, f"bars[{index}]")
        trade_date = _date(bar["trade_date"], "bar trade_date")
        known_at = bar["known_at"]
        if not isinstance(known_at, str):
            raise WorkerError("INVALID_OPERATION_INPUT", "bar known_at must be a timestamp")
        try:
            known_at_value = datetime.fromisoformat(known_at)
        except ValueError as exc:
            raise WorkerError("INVALID_OPERATION_INPUT", "bar known_at is invalid") from exc
        if known_at_value.tzinfo is None or (
            known_at_value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            != trade_date
        ):
            raise WorkerError(
                "POINT_IN_TIME_VIOLATION",
                "daily bar must become known on its own Shanghai trade date",
            )
        symbol = _token(bar["symbol"], "bar symbol", _SYMBOL_RE)
        if symbol not in instruments or trade_date not in calendar:
            raise WorkerError("INVALID_OPERATION_INPUT", "bar is outside instrument/calendar")
        key = (trade_date, symbol)
        if key in bars:
            raise WorkerError("INVALID_OPERATION_INPUT", "duplicate backtest bar")
        prices = {
            name: _integer(bar[name], f"bar.{name}", minimum=1)
            for name in (
                "open_fen",
                "high_fen",
                "low_fen",
                "close_fen",
                "signal_close_fen",
                "limit_reference_fen",
            )
        }
        if prices["low_fen"] > min(prices["open_fen"], prices["close_fen"]) or prices[
            "high_fen"
        ] < max(prices["open_fen"], prices["close_fen"]):
            raise WorkerError("INVALID_OPERATION_INPUT", "bar OHLC is inconsistent")
        status = bar["status"]
        if status not in {"traded", "suspended"}:
            raise WorkerError("INVALID_OPERATION_INPUT", "bar status is invalid")
        is_st = bar["is_st"]
        if not isinstance(is_st, bool):
            raise WorkerError("INVALID_OPERATION_INPUT", "bar is_st must be boolean")
        rule_id = _token(bar["market_rule_id"], "bar market_rule_id")
        rule = rules["market_rules"].get(rule_id)
        if rule is None:
            raise WorkerError("RULE_BINDING_ERROR", "bar references unknown market rule")
        instrument = instruments[symbol]
        if trade_date < instrument["listing_date"]:
            raise WorkerError(
                "INVALID_OPERATION_INPUT",
                "bar precedes the instrument listing date",
            )
        listing_trade_day_number = _integer(
            bar["listing_trade_day_number"],
            "bar.listing_trade_day_number",
            minimum=1,
        )
        if not (
            rule["effective_from"] <= trade_date <= rule["effective_to"]
            and rule["board"] == instrument["board"]
            and rule["is_st"] == is_st
            and listing_trade_day_number >= rule["listing_day_min"]
            and (
                rule["listing_day_max"] is None
                or listing_trade_day_number <= rule["listing_day_max"]
            )
        ):
            raise WorkerError("RULE_BINDING_ERROR", "market rule does not match dated instrument facts")
        features = bar["features"]
        if not isinstance(features, Mapping):
            raise WorkerError("INVALID_OPERATION_INPUT", "bar features must be an object")
        normalized_features = {
            _token(name, "feature name"): _number(value, f"feature {name}")
            for name, value in features.items()
        }
        bars[key] = {
            "trade_date": trade_date,
            "known_at": known_at_value.isoformat(),
            "symbol": symbol,
            **prices,
            "volume_shares": _integer(bar["volume_shares"], "volume_shares", minimum=0),
            "status": status,
            "is_st": is_st,
            "listing_trade_day_number": listing_trade_day_number,
            "market_rule_id": rule_id,
            "features": normalized_features,
        }

    actions: list[dict[str, Any]] = []
    action_keys = {
        "action_id",
        "symbol",
        "kind",
        "known_at",
        "record_date",
        "ex_date",
        "pay_date",
        "multiplier_numerator",
        "multiplier_denominator",
        "cash_per_share_fen",
    }
    action_ids: set[str] = set()
    for index, item in enumerate(root["corporate_actions"]):
        action = _exact(item, action_keys, f"corporate_actions[{index}]")
        kind = action["kind"]
        if kind not in {"share_split", "cash_dividend"}:
            raise WorkerError("UNSUPPORTED_SEMANTICS", "unsupported corporate action")
        symbol = _token(action["symbol"], "action symbol", _SYMBOL_RE)
        known_at = action["known_at"]
        if not isinstance(known_at, str):
            raise WorkerError("INVALID_OPERATION_INPUT", "action known_at must be a timestamp")
        try:
            known_at_value = datetime.fromisoformat(known_at)
        except ValueError as exc:
            raise WorkerError("INVALID_OPERATION_INPUT", "action known_at is invalid") from exc
        if known_at_value.tzinfo is None:
            raise WorkerError("INVALID_OPERATION_INPUT", "action known_at must be timezone-aware")
        record_date = _date(action["record_date"], "action record_date")
        ex_date = _date(action["ex_date"], "action ex_date")
        pay_date = _date(action["pay_date"], "action pay_date")
        if symbol not in instruments or not record_date <= ex_date <= pay_date:
            raise WorkerError("INVALID_OPERATION_INPUT", "corporate action is invalid")
        if known_at_value.date().isoformat() > ex_date:
            raise WorkerError(
                "POINT_IN_TIME_VIOLATION",
                "corporate action became known after its ex-date",
            )
        action_id = _token(action["action_id"], "action_id")
        if action_id in action_ids:
            raise WorkerError("INVALID_OPERATION_INPUT", "duplicate corporate action id")
        action_ids.add(action_id)
        actions.append(
            {
                "action_id": action_id,
                "symbol": symbol,
                "kind": kind,
                "known_at": known_at_value.isoformat(),
                "record_date": record_date,
                "ex_date": ex_date,
                "pay_date": pay_date,
                "multiplier_numerator": _integer(
                    action["multiplier_numerator"], "multiplier_numerator", minimum=1
                ),
                "multiplier_denominator": _integer(
                    action["multiplier_denominator"], "multiplier_denominator", minimum=1
                ),
                "cash_per_share_fen": _integer(
                    action["cash_per_share_fen"], "cash_per_share_fen", minimum=0
                ),
            }
        )
    actions.sort(key=lambda item: (item["ex_date"], item["action_id"]))
    dividend_windows: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for action in actions:
        if action["kind"] != "cash_dividend":
            continue
        windows = dividend_windows[action["symbol"]]
        if any(
            not (
                action["pay_date"] < existing_ex
                or action["ex_date"] > existing_pay
            )
            for existing_ex, existing_pay in windows
        ):
            raise WorkerError(
                "UNSUPPORTED_SEMANTICS",
                "overlapping dividend receivables for one symbol are unsupported",
            )
        windows.append((action["ex_date"], action["pay_date"]))
    return {
        "snapshot_sha256": root["snapshot_sha256"],
        "price_semantics": price_semantics,
        "rules": rules,
        "instruments": instruments,
        "calendar": calendar,
        "bars": bars,
        "actions": actions,
    }


def _parse_payload(
    payload: Mapping[str, Any],
    *,
    snapshot_sha256: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    request = _exact(
        payload,
        {
            "schema_version",
            "engine_request",
            "execution_plan",
            "data_snapshot_ref",
            "confirmation",
            "initial_cash_fen",
        },
        "backtest payload",
    )
    if request["schema_version"] != BACKTEST_REQUEST_SCHEMA:
        raise WorkerError("INVALID_OPERATION_INPUT", "unsupported backtest request schema")
    engine_request = request["engine_request"]
    plan = request["execution_plan"]
    snapshot_ref = request["data_snapshot_ref"]
    confirmation = _exact(
        request["confirmation"],
        {"version_id", "card_id", "receipt_id", "confirmation_hash"},
        "confirmation",
    )
    for name, value in confirmation.items():
        _token(value, f"confirmation.{name}")
    if not isinstance(engine_request, Mapping) or not isinstance(plan, Mapping):
        raise WorkerError("INVALID_OPERATION_INPUT", "request and plan must be objects")
    if engine_request.get("operation") != "backtest" or plan.get("operation") != "backtest":
        raise WorkerError("INVALID_OPERATION_INPUT", "operation must be backtest")
    if engine_request.get("request_id") is None:
        raise WorkerError("INVALID_OPERATION_INPUT", "EngineRequest request_id is missing")
    plan_material = dict(plan)
    plan_sha = plan_material.pop("content_sha256", None)
    plan_id = plan_material.pop("plan_id", None)
    expected_sha = _canonical_sha256(plan_material)
    if plan_sha != expected_sha or plan_id != f"strategy-plan:{expected_sha}":
        raise WorkerError("PLAN_IDENTITY_MISMATCH", "execution plan content identity is invalid")
    if (
        engine_request.get("strategy_spec_ref") != plan.get("strategy_spec_ref")
        or engine_request.get("data_snapshot_ref") != plan.get("data_snapshot_ref")
        or engine_request.get("engine") != plan.get("engine")
        or engine_request.get("resource_limits") != plan.get("resource_limits")
        or engine_request.get("random_seed") != plan.get("random_seed")
    ):
        raise WorkerError("PLAN_IDENTITY_MISMATCH", "EngineRequest and plan disagree")
    if snapshot_ref.get("snapshot_sha256") != snapshot_sha256:
        raise WorkerError("SNAPSHOT_HASH_MISMATCH", "DataSnapshotRef does not match snapshot bytes")
    if snapshot_ref.get("adjustment") != data["price_semantics"]["signal_price_adjustment"]:
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "DataSnapshotRef adjustment does not match signal price semantics",
        )
    if tuple(snapshot_ref.get("symbols", ())) != tuple(data["instruments"]):
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "DataSnapshotRef symbols do not match snapshot instruments",
        )
    if set(snapshot_ref.get("actual_sources", {})) != set(data["instruments"]):
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "DataSnapshotRef sources do not cover snapshot instruments",
        )
    if tuple(snapshot_ref.get("fields", ())) != (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ):
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "QE5-2 requires canonical OHLCVA snapshot fields",
        )
    return {
        "engine_request": engine_request,
        "plan": plan,
        "snapshot_ref": snapshot_ref,
        "confirmation": confirmation,
        "initial_cash_fen": _integer(
            request["initial_cash_fen"], "initial_cash_fen", minimum=0
        ),
        "backtest_input_sha256": _canonical_sha256(request),
    }


def _round_rate(notional_fen: int, tenths_bps: int) -> int:
    quotient, remainder = divmod(notional_fen * tenths_bps, 100_000)
    return quotient + int(remainder * 2 >= 100_000)


def _exact_scaled(value: Any, scale: int, label: str) -> int:
    numeric = _number(value, label)
    converted = round(numeric * scale)
    if abs(converted / scale - numeric) > 1e-9:
        raise WorkerError(
            "UNSUPPORTED_SEMANTICS",
            f"{label} cannot be represented at the required precision",
        )
    return converted


def _slipped_price(price_fen: int, tenths_bps: int, *, side: str) -> int:
    numerator = price_fen * (
        100_000 + tenths_bps if side == "buy" else 100_000 - tenths_bps
    )
    if numerator <= 0:
        raise WorkerError("UNSUPPORTED_SEMANTICS", "sell slippage makes price non-positive")
    quotient, remainder = divmod(numerator, 100_000)
    return max(1, quotient + int(remainder * 2 >= 100_000))


def _fees(shares: int, price_fen: int, side: str, rules: Mapping[str, Any]) -> int:
    notional = shares * price_fen
    commission = max(
        _round_rate(notional, rules["commission_tenths_bps"]),
        rules["minimum_commission_fen"],
    )
    transfer = _round_rate(notional, rules["transfer_fee_tenths_bps"])
    tax = _round_rate(notional, rules["sell_tax_tenths_bps"]) if side == "sell" else 0
    return commission + transfer + tax


def _limit_price(previous_close: int, bps: int, tick_fen: int, *, up: bool) -> int:
    numerator = previous_close * (10_000 + bps if up else 10_000 - bps)
    denominator = 10_000 * tick_fen
    ticks, remainder = divmod(numerator, denominator)
    if remainder * 2 >= denominator:
        ticks += 1
    return ticks * tick_fen


def _market_state(
    *,
    bar: Mapping[str, Any],
    instrument: Mapping[str, Any],
    rule: Mapping[str, Any],
) -> str:
    if instrument["delisting_date"] is not None and bar["trade_date"] > instrument["delisting_date"]:
        return "delisted"
    if bar["status"] == "suspended":
        return "suspended"
    if rule["limit_up_bps"] is None:
        return "open"
    upper = _limit_price(
        bar["limit_reference_fen"], rule["limit_up_bps"], rule["tick_fen"], up=True
    )
    lower = _limit_price(
        bar["limit_reference_fen"], rule["limit_down_bps"], rule["tick_fen"], up=False
    )
    if bar["open_fen"] == bar["high_fen"] == bar["low_fen"] == upper:
        return "locked_limit_up"
    if bar["open_fen"] == bar["high_fen"] == bar["low_fen"] == lower:
        return "locked_limit_down"
    return "open"


def _condition(
    operator: str,
    current: float,
    target: float,
    previous: float | None,
    previous_target: float | None,
) -> bool:
    if operator == "gt":
        return current > target
    if operator == "gte":
        return current >= target
    if operator == "lt":
        return current < target
    if operator == "lte":
        return current <= target
    if previous is None or previous_target is None:
        return False
    if operator == "crosses_above":
        return previous <= previous_target and current > target
    return previous >= previous_target and current < target


def _is_rebalance(frequency: str, current: str, next_date: str | None) -> bool:
    if next_date is None:
        return False
    if frequency == "daily":
        return True
    current_date = date.fromisoformat(current)
    following = date.fromisoformat(next_date)
    if frequency == "weekly":
        return current_date.isocalendar()[:2] != following.isocalendar()[:2]
    return (current_date.year, current_date.month) != (following.year, following.month)


def _factor_series(
    *,
    plan: Mapping[str, Any],
    data: Mapping[str, Any],
    engine: Mapping[str, Any],
) -> dict[tuple[str, str, str], float | None]:
    required = {
        item["field_id"]
        for item in plan["field_bindings"]
        if item["source"] == "quantaxis"
    }
    output: dict[tuple[str, str, str], float | None] = {}
    if not required:
        return output
    try:
        import pandas as pd
    except Exception as exc:
        raise WorkerError("ENGINE_IMPORT_ERROR", f"{type(exc).__name__}: {exc}") from exc
    for symbol in plan["strategy"]["universe_symbols"]:
        rows = [
            data["bars"][(trade_date, symbol)]
            for trade_date in data["calendar"]
            if (trade_date, symbol) in data["bars"]
        ]
        frame = pd.DataFrame(
            {"close": [item["signal_close_fen"] / 100 for item in rows]},
            index=pd.to_datetime([item["trade_date"] for item in rows]),
        )
        for field in required:
            name, raw_window = field.split("_", 1)
            window = int(raw_window)
            try:
                if name == "ma":
                    values = engine["indicators"].QA_indicator_MA(frame, window)[f"MA{window}"]
                else:
                    values = engine["indicators"].QA_indicator_EMA(frame, window)["EMA"]
            except Exception as exc:
                raise WorkerError(
                    "ENGINE_SEMANTIC_ERROR",
                    f"factor {field} failed: {type(exc).__name__}: {exc}",
                ) from exc
            for row, value in zip(rows, values, strict=True):
                numeric = None if pd.isna(value) else float(value)
                output[(row["trade_date"], symbol, field)] = numeric
    return output


def _backtest(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], dict[str, Any]],
) -> Mapping[str, Any]:
    data = _parse_snapshot(snapshot)
    request = _parse_payload(
        payload,
        snapshot_sha256=data["snapshot_sha256"],
        data=data,
    )
    plan = request["plan"]
    strategy = plan["strategy"]
    symbols = tuple(strategy["universe_symbols"])
    if set(symbols) - set(data["instruments"]):
        raise WorkerError("INCOMPLETE_SNAPSHOT", "strategy universe is missing instrument facts")
    as_of = request["snapshot_ref"]["as_of"]
    if any(
        trade_date > as_of for trade_date, _symbol in data["bars"]
    ) or any(
        datetime.fromisoformat(item["known_at"]).date().isoformat() > as_of
        for item in data["actions"]
    ):
        raise WorkerError(
            "POINT_IN_TIME_VIOLATION",
            "snapshot contains bars or corporate actions beyond as_of",
        )
    if strategy["execution"]["fill_price"] != "next_open" or strategy["execution"]["signal_lag_bars"] != 1:
        raise WorkerError("UNSUPPORTED_SEMANTICS", "QE5-2 supports next_open with one-bar lag")
    if strategy["execution"]["enforce_t_plus_one"] is not True:
        raise WorkerError("UNSUPPORTED_SEMANTICS", "A-share backtest must enforce T+1")
    try:
        engine = load_boundary()
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(
            "ENGINE_IMPORT_ERROR",
            f"cannot initialize QUANTAXIS factor boundary: {type(exc).__name__}: {exc}",
        ) from exc

    fee = data["rules"]["fee_schedule"]
    start_date = request["snapshot_ref"]["start_date"]
    end_date = request["snapshot_ref"]["end_date"]
    if not (fee["effective_from"] <= start_date and fee["effective_to"] >= end_date):
        raise WorkerError("RULE_BINDING_ERROR", "one fee schedule must cover the complete run")
    expected_fee = {
        "commission_tenths_bps": _exact_scaled(
            strategy["costs"]["commission_bps"],
            10,
            "commission_bps",
        ),
        "minimum_commission_fen": _exact_scaled(
            strategy["costs"]["minimum_commission"],
            100,
            "minimum_commission",
        ),
        "sell_tax_tenths_bps": _exact_scaled(
            strategy["costs"]["sell_tax_bps"],
            10,
            "sell_tax_bps",
        ),
        "transfer_fee_tenths_bps": _exact_scaled(
            strategy["costs"]["transfer_fee_bps"],
            10,
            "transfer_fee_bps",
        ),
        "rule_version": strategy["costs"]["rule_version"],
    }
    if any(fee[key] != value for key, value in expected_fee.items()):
        raise WorkerError("RULE_BINDING_ERROR", "confirmed costs do not match historical fee table")
    slippage_tenths_bps = _exact_scaled(
        strategy["costs"]["slippage_bps"],
        10,
        "slippage_bps",
    )
    risk_policy = {
        "max_drawdown_ppm": (
            None
            if strategy["risk"]["max_drawdown_stop"] is None
            else _exact_scaled(
                strategy["risk"]["max_drawdown_stop"],
                1_000_000,
                "max_drawdown_stop",
            )
        ),
        "max_purchase_turnover_ppm": (
            None
            if strategy["risk"]["max_turnover"] is None
            else _exact_scaled(
                strategy["risk"]["max_turnover"],
                1_000_000,
                "max_turnover",
            )
        ),
    }

    calendar = [
        item
        for item in data["calendar"]
        if start_date <= item <= min(end_date, strategy["evaluation"]["test_end"])
    ]
    if not calendar:
        raise WorkerError("INCOMPLETE_SNAPSHOT", "backtest calendar is empty")
    if calendar[0] < start_date or calendar[-1] > end_date:
        raise WorkerError("SNAPSHOT_SEMANTICS_MISMATCH", "calendar exceeds snapshot dates")
    factors = _factor_series(plan=plan, data=data, engine=engine)
    values: dict[tuple[str, str, str], float | None] = {}
    for trade_date in calendar:
        for symbol in symbols:
            bar = data["bars"].get((trade_date, symbol))
            if bar is None:
                continue
            values[(trade_date, symbol, "close")] = bar["signal_close_fen"] / 100
            for name, value in bar["features"].items():
                values[(trade_date, symbol, name)] = value
    values.update(factors)

    initial_cash = request["initial_cash_fen"]
    cash = initial_cash
    receivables: dict[str, int] = {}
    entitlements: dict[str, int] = {}
    lots: dict[str, list[list[Any]]] = defaultdict(list)
    events: list[dict[str, Any]] = []
    position_snapshots: list[dict[str, Any]] = []
    signal_audit: list[dict[str, Any]] = []
    risk_audit: list[dict[str, Any]] = []
    pending_symbols: list[str] | None = None
    cumulative_purchase_notional_fen = 0
    peak_equity_fen = initial_cash
    drawdown_halted = False

    def positions() -> dict[str, int]:
        return {
            symbol: sum(int(lot[1]) for lot in symbol_lots)
            for symbol, symbol_lots in lots.items()
            if symbol_lots
        }

    def marks(
        trade_date: str,
        *,
        price_field: str = "close_fen",
    ) -> dict[str, int]:
        return {
            symbol: data["bars"][(trade_date, symbol)][price_field]
            for symbol in positions()
            if (trade_date, symbol) in data["bars"]
        }

    for day_index, trade_date in enumerate(calendar):
        bars = {
            symbol: data["bars"][(trade_date, symbol)]
            for symbol in symbols
            if (trade_date, symbol) in data["bars"]
        }
        if set(positions()) - set(bars):
            raise WorkerError("INCOMPLETE_SNAPSHOT", "held position has no daily bar")

        for action in [item for item in data["actions"] if item["ex_date"] == trade_date]:
            held = entitlements.get(action["action_id"], 0)
            if held == 0:
                continue
            if action["kind"] == "share_split" and positions().get(action["symbol"], 0) != held:
                raise WorkerError(
                    "UNSUPPORTED_SEMANTICS",
                    "share entitlement differs from the held ex-date position",
                )
            action_marks = marks(trade_date, price_field="open_fen")
            if action["kind"] == "share_split":
                next_lots = []
                for acquired, quantity in lots[action["symbol"]]:
                    product = quantity * action["multiplier_numerator"]
                    next_quantity, remainder = divmod(product, action["multiplier_denominator"])
                    if remainder:
                        raise WorkerError("UNSUPPORTED_SEMANTICS", "share split creates fractional shares")
                    next_lots.append([acquired, next_quantity])
                lots[action["symbol"]] = next_lots
                events.append(
                    {
                        "event": "share_split",
                        "trade_date": trade_date,
                        "order": None,
                        "mark_prices_fen": action_marks,
                        "symbol": action["symbol"],
                        "multiplier_numerator": action["multiplier_numerator"],
                        "multiplier_denominator": action["multiplier_denominator"],
                        "entitled_shares": None,
                        "cash_per_share_fen": None,
                        "market_rule_id": None,
                    }
                )
            elif action["cash_per_share_fen"] > 0:
                amount = held * action["cash_per_share_fen"]
                receivables[action["symbol"]] = receivables.get(action["symbol"], 0) + amount
                events.append(
                    {
                        "event": "dividend_ex",
                        "trade_date": trade_date,
                        "order": None,
                        "mark_prices_fen": action_marks,
                        "symbol": action["symbol"],
                        "multiplier_numerator": None,
                        "multiplier_denominator": None,
                        "entitled_shares": held,
                        "cash_per_share_fen": action["cash_per_share_fen"],
                        "market_rule_id": None,
                    }
                )
        for action in [item for item in data["actions"] if item["pay_date"] == trade_date]:
            amount = receivables.pop(action["symbol"], 0)
            if amount:
                cash += amount
                events.append(
                    {
                        "event": "dividend_pay",
                        "trade_date": trade_date,
                        "order": None,
                        "mark_prices_fen": marks(
                            trade_date,
                            price_field="open_fen",
                        ),
                        "symbol": action["symbol"],
                        "multiplier_numerator": None,
                        "multiplier_denominator": None,
                        "entitled_shares": None,
                        "cash_per_share_fen": None,
                        "market_rule_id": None,
                    }
                )

        if pending_symbols is not None:
            current = positions()
            equity_at_open = cash + sum(
                quantity * bars[symbol]["open_fen"]
                for symbol, quantity in current.items()
            ) + sum(receivables.values())
            pending_targets: dict[str, int] = {}
            if pending_symbols:
                weight = min(
                    (1 - strategy["portfolio"]["cash_buffer_weight"])
                    / len(pending_symbols),
                    strategy["portfolio"]["max_position_weight"],
                )
                for symbol in pending_symbols:
                    lot = strategy["execution"]["board_lot"]
                    pending_targets[symbol] = (
                        int(equity_at_open * weight / bars[symbol]["open_fen"])
                        // lot
                        * lot
                    )
            order_sides = (
                [
                    (symbol, "sell", current[symbol] - pending_targets.get(symbol, 0))
                    for symbol in sorted(current)
                    if current[symbol] > pending_targets.get(symbol, 0)
                ]
                + [
                    (symbol, "buy", pending_targets[symbol] - current.get(symbol, 0))
                    for symbol in sorted(pending_targets)
                    if pending_targets[symbol] > current.get(symbol, 0)
                ]
            )
            for order_index, (symbol, side, requested_shares) in enumerate(order_sides):
                bar = bars[symbol]
                rule = data["rules"]["market_rules"][bar["market_rule_id"]]
                state = _market_state(
                    bar=bar,
                    instrument=data["instruments"][symbol],
                    rule=rule,
                )
                price_fen = _slipped_price(
                    bar["open_fen"],
                    slippage_tenths_bps,
                    side=side,
                )
                capacity = (
                    bar["volume_shares"] * data["rules"]["max_participation_bps"] // 10_000
                )
                if side == "buy":
                    turnover_limit = risk_policy["max_purchase_turnover_ppm"]
                    if turnover_limit is not None:
                        turnover_budget_fen = (
                            initial_cash * turnover_limit // 1_000_000
                        )
                        remaining_budget_fen = max(
                            0,
                            turnover_budget_fen - cumulative_purchase_notional_fen,
                        )
                        capacity = min(capacity, remaining_budget_fen // price_fen)
                    capacity = (
                        capacity
                        // strategy["execution"]["board_lot"]
                        * strategy["execution"]["board_lot"]
                    )
                order = {
                    "order_id": f"{request['engine_request']['request_id']}:{trade_date}:{order_index}",
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "side": side,
                    "requested_shares": requested_shares,
                    "price_fen": price_fen,
                    "market_state": state,
                    "maximum_fill_shares": capacity,
                }
                events.append(
                    {
                        "event": "order",
                        "trade_date": trade_date,
                        "order": order,
                        "mark_prices_fen": None,
                        "symbol": None,
                        "multiplier_numerator": None,
                        "multiplier_denominator": None,
                        "entitled_shares": None,
                        "cash_per_share_fen": None,
                        "market_rule_id": bar["market_rule_id"],
                    }
                )
                blocked = (
                    state in {"suspended", "delisted"}
                    or (state == "locked_limit_up" and side == "buy")
                    or (state == "locked_limit_down" and side == "sell")
                )
                filled = min(requested_shares, capacity)
                if side == "buy":
                    lot = strategy["execution"]["board_lot"]
                    filled = filled // lot * lot
                    total = filled * price_fen + (_fees(filled, price_fen, side, fee) if filled else 0)
                    if blocked or filled == 0 or total > cash:
                        filled = 0
                    if filled:
                        cash -= total
                        cumulative_purchase_notional_fen += filled * price_fen
                        lots[symbol].append([trade_date, filled])
                else:
                    sellable = sum(
                        quantity
                        for acquired, quantity in lots.get(symbol, [])
                        if acquired < trade_date
                    )
                    if blocked or requested_shares > sellable:
                        filled = 0
                    if filled:
                        remaining = filled
                        next_lots = []
                        for acquired, quantity in lots[symbol]:
                            consumed = min(quantity, remaining) if acquired < trade_date else 0
                            remaining -= consumed
                            if quantity - consumed:
                                next_lots.append([acquired, quantity - consumed])
                        if next_lots:
                            lots[symbol] = next_lots
                        else:
                            lots.pop(symbol, None)
                        cash += filled * price_fen - _fees(filled, price_fen, side, fee)
            pending_symbols = None

        for action in [
            item for item in data["actions"] if item["record_date"] == trade_date
        ]:
            entitlements[action["action_id"]] = positions().get(action["symbol"], 0)

        day_marks = marks(trade_date)
        events.append(
            {
                "event": "mark",
                "trade_date": trade_date,
                "order": None,
                "mark_prices_fen": day_marks,
                "symbol": None,
                "multiplier_numerator": None,
                "multiplier_denominator": None,
                "entitled_shares": None,
                "cash_per_share_fen": None,
                "market_rule_id": None,
            }
        )
        equity_fen = (
            cash
            + sum(receivables.values())
            + sum(
                quantity * day_marks[symbol]
                for symbol, quantity in positions().items()
            )
        )
        peak_equity_fen = max(peak_equity_fen, equity_fen)
        drawdown_limit = risk_policy["max_drawdown_ppm"]
        drawdown_ppm = (
            0
            if peak_equity_fen == 0
            else (peak_equity_fen - equity_fen) * 1_000_000 // peak_equity_fen
        )
        if (
            drawdown_limit is not None
            and drawdown_ppm >= drawdown_limit
        ):
            drawdown_halted = True
        purchase_turnover_ppm = (
            0
            if initial_cash == 0
            else cumulative_purchase_notional_fen * 1_000_000 // initial_cash
        )
        position_snapshots.append(
            {
                "trade_date": trade_date,
                "positions": positions(),
                "cash_fen": cash,
                "dividend_receivable_fen": sum(receivables.values()),
                "equity_fen": equity_fen,
            }
        )
        risk_audit.append(
            {
                "trade_date": trade_date,
                "peak_equity_fen": peak_equity_fen,
                "drawdown_ppm": drawdown_ppm,
                "cumulative_purchase_notional_fen": cumulative_purchase_notional_fen,
                "purchase_turnover_ppm": purchase_turnover_ppm,
                "drawdown_halted": drawdown_halted,
            }
        )

        next_date = calendar[day_index + 1] if day_index + 1 < len(calendar) else None
        if drawdown_halted:
            pending_symbols = [] if next_date is not None else None
            continue
        if _is_rebalance(strategy["execution"]["rebalance"], trade_date, next_date):
            eligible: list[str] = []
            audit_symbols: dict[str, Any] = {}
            for symbol in symbols:
                if symbol not in bars:
                    continue
                passed = True
                rule_audit = []
                for rule in strategy["signals"]:
                    current_value = values.get((trade_date, symbol, rule["field"]))
                    target = rule["value"]
                    if isinstance(target, str):
                        target_value = values.get((trade_date, symbol, target))
                    else:
                        target_value = float(target)
                    outcome = True
                    consecutive = rule["consecutive_days"]
                    for offset in range(consecutive):
                        history_index = day_index - offset
                        if history_index < 0:
                            outcome = False
                            break
                        history_date = calendar[history_index]
                        history_value = values.get(
                            (history_date, symbol, rule["field"])
                        )
                        history_target = (
                            values.get((history_date, symbol, target))
                            if isinstance(target, str)
                            else float(target)
                        )
                        prior_date = (
                            calendar[history_index - 1]
                            if history_index > 0
                            else None
                        )
                        previous_value = (
                            values.get((prior_date, symbol, rule["field"]))
                            if prior_date is not None
                            else None
                        )
                        previous_target = (
                            values.get((prior_date, symbol, target))
                            if prior_date is not None and isinstance(target, str)
                            else (
                                float(target)
                                if prior_date is not None
                                else None
                            )
                        )
                        outcome = bool(
                            outcome
                            and history_value is not None
                            and history_target is not None
                            and _condition(
                                rule["operator"],
                                history_value,
                                history_target,
                                previous_value,
                                previous_target,
                            )
                        )
                        if not outcome:
                            break
                    rule_audit.append(
                        {
                            "field": rule["field"],
                            "operator": rule["operator"],
                            "value": current_value,
                            "target": target_value,
                            "passed": outcome,
                        }
                    )
                    passed = passed and outcome
                audit_symbols[symbol] = rule_audit
                if passed:
                    eligible.append(symbol)
            ranking = strategy["ranking"]
            if ranking is not None:
                eligible = [
                    symbol
                    for symbol in eligible
                    if values.get((trade_date, symbol, ranking["field"])) is not None
                ]
                eligible.sort(
                    key=lambda symbol: (
                        (
                            -float(values[(trade_date, symbol, ranking["field"])])
                            if ranking["direction"] == "descending"
                            else float(values[(trade_date, symbol, ranking["field"])])
                        ),
                        symbol,
                    )
                )
                eligible = eligible[: ranking["top_n"]]
            eligible = eligible[: strategy["portfolio"]["max_positions"]]
            if next_date is not None and any(
                (next_date, symbol) not in data["bars"] for symbol in eligible
            ):
                raise WorkerError(
                    "INCOMPLETE_SNAPSHOT",
                    "selected symbol has no next-open execution bar",
                )
            pending_symbols = list(eligible)
            audited_rules = {
                symbol: audit_symbols[symbol]
                for symbol in eligible
                if symbol in audit_symbols
            }
            signal_audit.append(
                {
                    "trade_date": trade_date,
                    "execute_date": next_date,
                    "selected_symbols": eligible,
                    "evaluated_symbol_count": len(audit_symbols),
                    "eligible_symbol_count": len(eligible),
                    "rule_outcomes_sha256": _canonical_sha256(audit_symbols),
                    "rules": audited_rules,
                    "rules_truncated": len(audited_rules) < len(audit_symbols),
                }
            )
    fee_result = {
        key: fee[key]
        for key in (
            "commission_tenths_bps",
            "minimum_commission_fen",
            "sell_tax_tenths_bps",
            "transfer_fee_tenths_bps",
            "rule_version",
        )
    }
    return {
        "operation_schema": BACKTEST_RESULT_SCHEMA,
        "snapshot_sha256": data["snapshot_sha256"],
        "engine_request_id": request["engine_request"]["request_id"],
        "execution_plan_sha256": plan["content_sha256"],
        "backtest_input_sha256": request["backtest_input_sha256"],
        "rule_table_version": data["rules"]["version"],
        "fee_schedule": fee_result,
        "slippage_tenths_bps": slippage_tenths_bps,
        "risk_policy": risk_policy,
        "opening_date": calendar[0],
        "initial_cash_fen": initial_cash,
        "board_lot": strategy["execution"]["board_lot"],
        "events": events,
        "engine_position_snapshots": position_snapshots,
        "signal_audit": signal_audit,
        "risk_audit": risk_audit,
        "engine_version": engine["version"],
        "source_sha256": engine["source_sha256"],
    }


def build_backtest_handler(
    load_boundary: Callable[[], dict[str, Any]],
) -> Callable[[Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]]:
    return lambda payload, snapshot: _backtest(payload, snapshot, load_boundary)
