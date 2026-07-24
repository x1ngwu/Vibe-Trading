"""Strict QE2 operations over one content-bound, offline QUANTAXIS snapshot."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from worker_runtime import WorkerError


OPERATION_SNAPSHOT_SCHEMA = "vibe.quantaxis-operation-snapshot.v1"
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BAR_KEYS = {"trade_date", "open", "high", "low", "close", "volume", "amount"}
_ACTION_KEYS = {
    "ex_date",
    "known_at",
    "category",
    "fenhong",
    "peigu",
    "peigujia",
    "songzhuangu",
}


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} keys do not match schema")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive finite" if positive else "finite"
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be {qualifier}")
    return result


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise WorkerError("INVALID_OPERATION_INPUT", f"{label} is not a valid date") from exc
    return value


def _load_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        raise WorkerError("SNAPSHOT_REQUIRED", "formal QUANTAXIS operations require a snapshot")
    path = Path(str(snapshot["path"]))
    if not path.is_file():
        raise WorkerError("INVALID_OPERATION_INPUT", "operation snapshot must be one JSON file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError("INVALID_OPERATION_INPUT", "operation snapshot is not valid UTF-8 JSON") from exc
    root = _exact(
        value,
        {"schema_version", "symbol", "as_of", "bars", "corporate_actions", "calendar"},
        "operation snapshot",
    )
    if root["schema_version"] != OPERATION_SNAPSHOT_SCHEMA:
        raise WorkerError("INVALID_OPERATION_INPUT", "unsupported operation snapshot schema")
    symbol = root["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise WorkerError("INVALID_OPERATION_INPUT", "snapshot symbol is invalid")
    as_of = _date(root["as_of"], "snapshot.as_of")

    bars = root["bars"]
    if not isinstance(bars, list) or not bars:
        raise WorkerError("INVALID_OPERATION_INPUT", "snapshot.bars must be a non-empty array")
    normalized_bars: list[dict[str, Any]] = []
    for index, item in enumerate(bars):
        row = _exact(item, _BAR_KEYS, f"snapshot.bars[{index}]")
        trade_date = _date(row["trade_date"], f"snapshot.bars[{index}].trade_date")
        normalized = {
            "trade_date": trade_date,
            **{
                field: _number(
                    row[field],
                    f"snapshot.bars[{index}].{field}",
                    positive=field in {"open", "high", "low", "close"},
                )
                for field in ("open", "high", "low", "close", "volume", "amount")
            },
        }
        if normalized["volume"] < 0 or normalized["amount"] < 0:
            raise WorkerError("INVALID_OPERATION_INPUT", "volume and amount must be non-negative")
        if (
            normalized["high"] < max(normalized["open"], normalized["close"])
            or normalized["low"] > min(normalized["open"], normalized["close"])
            or normalized["low"] > normalized["high"]
        ):
            raise WorkerError("INVALID_OPERATION_INPUT", f"invalid OHLC at {trade_date}")
        normalized_bars.append(normalized)
    bar_dates = [item["trade_date"] for item in normalized_bars]
    if bar_dates != sorted(bar_dates) or len(bar_dates) != len(set(bar_dates)):
        raise WorkerError("INVALID_OPERATION_INPUT", "bar dates must be unique and sorted")
    if bar_dates[-1] > as_of:
        raise WorkerError("INVALID_OPERATION_INPUT", "bars extend beyond snapshot.as_of")

    actions = root["corporate_actions"]
    if not isinstance(actions, list):
        raise WorkerError("INVALID_OPERATION_INPUT", "snapshot.corporate_actions must be an array")
    normalized_actions: list[dict[str, Any]] = []
    for index, item in enumerate(actions):
        action = _exact(item, _ACTION_KEYS, f"snapshot.corporate_actions[{index}]")
        ex_date = _date(action["ex_date"], f"snapshot.corporate_actions[{index}].ex_date")
        known_at = action["known_at"]
        if not isinstance(known_at, str):
            raise WorkerError("INVALID_OPERATION_INPUT", "corporate action known_at must be a timestamp")
        try:
            parsed_known_at = datetime.fromisoformat(known_at)
        except ValueError as exc:
            raise WorkerError("INVALID_OPERATION_INPUT", "corporate action known_at is invalid") from exc
        if parsed_known_at.tzinfo is None or parsed_known_at.date().isoformat() > as_of:
            raise WorkerError(
                "INVALID_OPERATION_INPUT",
                "corporate action must be timezone-aware and known by snapshot.as_of",
            )
        if action["category"] != 1:
            raise WorkerError("UNSUPPORTED_SEMANTICS", "only QUANTAXIS category=1 actions are supported")
        normalized = {
            "ex_date": ex_date,
            "category": 1,
            **{
                field: _number(action[field], f"snapshot.corporate_actions[{index}].{field}")
                for field in ("fenhong", "peigu", "peigujia", "songzhuangu")
            },
        }
        if any(normalized[field] < 0 for field in ("fenhong", "peigu", "peigujia", "songzhuangu")):
            raise WorkerError("INVALID_OPERATION_INPUT", "corporate action values must be non-negative")
        normalized_actions.append(normalized)
    action_dates = [item["ex_date"] for item in normalized_actions]
    if action_dates != sorted(action_dates) or len(action_dates) != len(set(action_dates)):
        raise WorkerError("INVALID_OPERATION_INPUT", "corporate action dates must be unique and sorted")
    if any(item > as_of or item not in set(bar_dates) for item in action_dates):
        raise WorkerError(
            "INVALID_OPERATION_INPUT", "corporate actions must fall on a snapshot bar by as_of"
        )

    calendar = root["calendar"]
    if not isinstance(calendar, list) or not calendar:
        raise WorkerError("INVALID_OPERATION_INPUT", "snapshot.calendar must be a non-empty array")
    normalized_calendar: list[dict[str, Any]] = []
    for index, item in enumerate(calendar):
        day = _exact(item, {"trade_date", "is_open", "reason"}, f"snapshot.calendar[{index}]")
        trade_date = _date(day["trade_date"], f"snapshot.calendar[{index}].trade_date")
        if not isinstance(day["is_open"], bool) or day["reason"] not in {
            "trading_day",
            "weekend",
            "holiday",
        }:
            raise WorkerError("INVALID_OPERATION_INPUT", "calendar day has invalid state")
        if day["is_open"] != (day["reason"] == "trading_day"):
            raise WorkerError("INVALID_OPERATION_INPUT", "calendar is_open disagrees with reason")
        normalized_calendar.append(
            {"trade_date": trade_date, "is_open": day["is_open"], "reason": day["reason"]}
        )
    calendar_dates = [item["trade_date"] for item in normalized_calendar]
    if calendar_dates != sorted(calendar_dates) or len(calendar_dates) != len(set(calendar_dates)):
        raise WorkerError("INVALID_OPERATION_INPUT", "calendar dates must be unique and sorted")
    if calendar_dates[-1] > as_of:
        raise WorkerError("INVALID_OPERATION_INPUT", "calendar extends beyond snapshot.as_of")

    return {
        "symbol": symbol,
        "as_of": as_of,
        "bars": normalized_bars,
        "corporate_actions": normalized_actions,
        "calendar": normalized_calendar,
        "snapshot_sha256": str(snapshot["sha256"]),
    }


def _boundary(load_boundary: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return load_boundary()
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("ENGINE_IMPORT_ERROR", f"{type(exc).__name__}: {exc}") from exc


def _rounded(value: Any) -> float | None:
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    result = float(value)
    return round(result, 10) if math.isfinite(result) else None


def _adjust_prices(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], dict[str, Any]],
) -> Mapping[str, Any]:
    request = _exact(payload, {"symbol", "adjustment"}, "adjust_prices payload")
    if request["adjustment"] not in {"qfq", "hfq"}:
        raise WorkerError("INVALID_OPERATION_INPUT", "adjustment must be qfq or hfq")
    data = _load_snapshot(snapshot)
    if request["symbol"] != data["symbol"]:
        raise WorkerError("INVALID_OPERATION_INPUT", "payload symbol does not match snapshot")

    import pandas as pd

    engine = _boundary(load_boundary)
    bars = pd.DataFrame(data["bars"])
    bars.index = pd.to_datetime(bars.pop("trade_date"))
    actions = pd.DataFrame(data["corporate_actions"])
    if actions.empty:
        actions = pd.DataFrame(
            columns=["category", "fenhong", "peigu", "peigujia", "songzhuangu"],
            index=pd.DatetimeIndex([], name="ex_date"),
        )
    else:
        actions.index = pd.to_datetime(actions.pop("ex_date"))
    try:
        adjusted = engine["data_fq"]._QA_data_stock_to_fq(
            bars.copy(), actions.copy(), request["adjustment"]
        )
    except Exception as exc:
        raise WorkerError("ENGINE_SEMANTIC_ERROR", f"adjust_prices failed: {type(exc).__name__}: {exc}") from exc
    if list(adjusted.index) != list(bars.index):
        raise WorkerError("ENGINE_SEMANTIC_ERROR", "QUANTAXIS changed the bar date set")

    share_factors: list[float] = []
    cumulative = 1.0
    actions_by_date = {item["ex_date"]: item for item in data["corporate_actions"]}
    for trade_date in (item["trade_date"] for item in data["bars"]):
        action = actions_by_date.get(trade_date)
        if action is not None:
            cumulative *= (10.0 + action["peigu"] + action["songzhuangu"]) / 10.0
        share_factors.append(cumulative)
    anchor_factor = share_factors[-1] if request["adjustment"] == "qfq" else share_factors[0]

    rows = []
    for index, (trade_date, row) in enumerate(adjusted.iterrows()):
        volume_multiplier = anchor_factor / share_factors[index]
        rows.append(
            {
                "trade_date": trade_date.date().isoformat(),
                **{field: _rounded(row[field]) for field in ("open", "high", "low", "close")},
                "volume": _rounded(bars.iloc[index]["volume"] * volume_multiplier),
                "amount": _rounded(bars.iloc[index]["amount"]),
            }
        )
    anchor_date = rows[-1]["trade_date"] if request["adjustment"] == "qfq" else rows[0]["trade_date"]
    return {
        "operation_schema": "vibe.quantaxis-adjust-prices-result.v1",
        "snapshot_sha256": data["snapshot_sha256"],
        "symbol": data["symbol"],
        "adjustment": request["adjustment"],
        "price_anchor_date": anchor_date,
        "volume_anchor_date": anchor_date,
        "amount_adjustment": "none",
        "rows": rows,
        "engine_version": engine["version"],
        "source_sha256": engine["source_sha256"],
    }


def _trading_calendar(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], dict[str, Any]],
) -> Mapping[str, Any]:
    request = _exact(payload, {"start_date", "end_date"}, "trading_calendar payload")
    start_date = _date(request["start_date"], "start_date")
    end_date = _date(request["end_date"], "end_date")
    if start_date > end_date:
        raise WorkerError("INVALID_OPERATION_INPUT", "start_date must not follow end_date")
    data = _load_snapshot(snapshot)
    selected = [item for item in data["calendar"] if start_date <= item["trade_date"] <= end_date]
    expected_dates: list[str] = []
    cursor = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while cursor <= end:
        expected_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if [item["trade_date"] for item in selected] != expected_dates:
        raise WorkerError("INCOMPLETE_SNAPSHOT", "snapshot calendar does not cover every requested date")

    engine = _boundary(load_boundary)
    upstream_open = set(engine["calendar"].trade_date_sse)
    mismatches = [
        {
            "trade_date": item["trade_date"],
            "snapshot_is_open": item["is_open"],
            "quantaxis_is_open": item["trade_date"] in upstream_open,
        }
        for item in selected
        if item["is_open"] != (item["trade_date"] in upstream_open)
    ]
    return {
        "operation_schema": "vibe.quantaxis-trading-calendar-result.v1",
        "snapshot_sha256": data["snapshot_sha256"],
        "truth_source": "content_bound_snapshot",
        "days": selected,
        "open_dates": [item["trade_date"] for item in selected if item["is_open"]],
        "quantaxis_oracle_mismatches": mismatches,
        "engine_version": engine["version"],
        "source_sha256": engine["source_sha256"],
    }


def _compute_factors(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], dict[str, Any]],
) -> Mapping[str, Any]:
    request = _exact(payload, {"symbol", "factors"}, "compute_factors payload")
    data = _load_snapshot(snapshot)
    if request["symbol"] != data["symbol"]:
        raise WorkerError("INVALID_OPERATION_INPUT", "payload symbol does not match snapshot")
    factors = request["factors"]
    if not isinstance(factors, list) or not factors or len(factors) > 16:
        raise WorkerError("INVALID_OPERATION_INPUT", "factors must contain 1 to 16 specifications")
    specs: list[tuple[str, int]] = []
    for index, item in enumerate(factors):
        spec = _exact(item, {"name", "window"}, f"factors[{index}]")
        if spec["name"] not in {"ma", "ema"}:
            raise WorkerError("UNSUPPORTED_FACTOR", f"unsupported factor: {spec['name']}")
        window = spec["window"]
        if isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= 512:
            raise WorkerError("INVALID_OPERATION_INPUT", "factor window must be an integer in [2, 512]")
        specs.append((spec["name"], window))
    if len(set(specs)) != len(specs):
        raise WorkerError("INVALID_OPERATION_INPUT", "factor specifications must be unique")

    import pandas as pd

    engine = _boundary(load_boundary)
    bars = pd.DataFrame(data["bars"])
    bars.index = pd.to_datetime(bars.pop("trade_date"))
    output: dict[str, Any] = {}
    for name, window in specs:
        column = f"{name}_{window}"
        try:
            if name == "ma":
                values = engine["indicators"].QA_indicator_MA(bars, window)[f"MA{window}"]
            else:
                values = engine["indicators"].QA_indicator_EMA(bars, window)["EMA"]
        except Exception as exc:
            raise WorkerError(
                "ENGINE_SEMANTIC_ERROR",
                f"factor {column} failed: {type(exc).__name__}: {exc}",
            ) from exc
        output[column] = list(values)
    rows = [
        {
            "trade_date": item["trade_date"],
            **{column: _rounded(values[index]) for column, values in output.items()},
        }
        for index, item in enumerate(data["bars"])
    ]
    return {
        "operation_schema": "vibe.quantaxis-factor-result.v1",
        "snapshot_sha256": data["snapshot_sha256"],
        "symbol": data["symbol"],
        "input_price_basis": "raw",
        "factors": [{"name": name, "window": window} for name, window in specs],
        "rows": rows,
        "engine_version": engine["version"],
        "source_sha256": engine["source_sha256"],
    }


def build_formal_handlers(
    load_boundary: Callable[[], dict[str, Any]],
) -> dict[str, Callable[[Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]]]:
    """Bind the audited leaf-module loader to the strict operation handlers."""

    return {
        "adjust_prices": lambda payload, snapshot: _adjust_prices(payload, snapshot, load_boundary),
        "trading_calendar": lambda payload, snapshot: _trading_calendar(payload, snapshot, load_boundary),
        "compute_factors": lambda payload, snapshot: _compute_factors(payload, snapshot, load_boundary),
    }
