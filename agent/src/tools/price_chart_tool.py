"""Persist chat-native candlestick visualizations for the current run."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.market_data import YAHOO_INDEX_SYMBOLS, detect_source, fetch_market_data, get_loader
from src.tools.path_utils import safe_path, safe_run_dir


_MAX_SYMBOLS = 5
_MAX_BARS = 5000
_MANIFEST_NAME = "visualizations.json"
_SUPPORTED_INTERVALS = ("1m", "5m", "15m", "30m", "1H", "1D")
_INTRADAY_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1H"})
_RETENTION_POLICY = "latest_contiguous_up_to_5000_bars"
_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60}
_SESSION_MINUTES = {
    "A-share": 240,
    "HK": 330,
    "US": 390,
    "India": 375,
    "Crypto": 1440,
    "Unknown": 1440,
}
# Yahoo rejects intraday windows beyond these documented availability ranges.
# Values are inclusive-date spans: six means at most seven calendar dates.
_PROVIDER_MAX_INTRADAY_SPAN_DAYS = {
    "yahoo": {"1m": 6, "5m": 59, "15m": 59, "30m": 59, "1H": 729},
    "yfinance": {"1m": 6, "5m": 59, "15m": 59, "30m": 59, "1H": 729},
}
_DEFAULT_LOOKBACK_DAYS = {
    "1m": 3,
    "5m": 14,
    "15m": 60,
    "30m": 60,
    "1H": 180,
    "1D": 1825,
}


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol.isdigit() and len(symbol) == 6:
        if symbol[0] in {"6", "9"}:
            return f"{symbol}.SH"
        if symbol[0] in {"0", "3"}:
            return f"{symbol}.SZ"
        if symbol[0] in {"4", "8"}:
            return f"{symbol}.BJ"
    return symbol


def _market_for(symbol: str) -> str:
    if symbol in YAHOO_INDEX_SYMBOLS:
        return "US"
    if symbol.endswith((".SH", ".SZ", ".BJ")):
        return "A-share"
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith(".US"):
        return "US"
    if symbol.endswith((".NS", ".BO")):
        return "India"
    if symbol.endswith("-USDT") or "/USDT" in symbol:
        return "Crypto"
    return "Unknown"


def _adjustment_for(source: str) -> str:
    if source in {"tencent", "akshare", "eastmoney"}:
        return "qfq"
    if source in {"yahoo", "yfinance", "okx", "ccxt", "stooq"}:
        return "raw"
    return "provider_default"


def _timezone_for(symbol: str, source: str) -> str:
    """Describe the timezone used by the loader's timestamp strings."""
    if source in {"yahoo", "okx", "ccxt"}:
        return "UTC"
    if symbol.endswith((".SH", ".SZ", ".BJ")):
        return "Asia/Shanghai"
    if symbol.endswith(".HK"):
        return "Asia/Hong_Kong"
    if symbol.endswith(".US"):
        return "America/New_York"
    return "provider-local"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_interval(value: Any) -> str:
    raw = str(value or "1D").strip()
    if raw in _SUPPORTED_INTERVALS:
        return raw
    if raw == "1M":
        raise ValueError("unsupported interval '1M'; use '1m' for one-minute bars")
    aliases = {
        "1min": "1m",
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "60m": "1H",
        "60min": "1H",
        "1h": "1H",
        "1d": "1D",
        "d": "1D",
        "day": "1D",
        "daily": "1D",
    }
    normalized = aliases.get(raw.lower())
    if normalized:
        return normalized
    supported = ", ".join(_SUPPORTED_INTERVALS)
    raise ValueError(f"unsupported interval {raw!r}; choose one of: {supported}")


def _normalize_time(value: Any, interval: str) -> tuple[str, float] | None:
    text = str(value).strip()
    if not text:
        return None
    if interval in _INTRADAY_INTERVALS:
        normalized = text.replace(" ", "T", 1)
        if "T" not in normalized:
            return None
        parse_text = (
            normalized[:-1] + "+00:00"
            if normalized.endswith("Z")
            else normalized
        )
        try:
            parsed = datetime.fromisoformat(parse_text)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
            normalized = parsed.isoformat()
            sort_value = parsed.timestamp()
        else:
            normalized = parsed.isoformat()
            sort_value = parsed.replace(tzinfo=timezone.utc).timestamp()
        return normalized, sort_value

    date_text = text.split("T", 1)[0].split(" ", 1)[0]
    try:
        day = date.fromisoformat(date_text)
    except ValueError:
        return None
    return day.isoformat(), float(day.toordinal())


def _normalize_bars(rows: Any, interval: str) -> tuple[list[dict[str, Any]], bool, int]:
    if isinstance(rows, dict) and isinstance(rows.get("data"), list):
        rows = rows["data"]
    if not isinstance(rows, list):
        return [], False, 0

    bars_by_time: dict[str, tuple[float, dict[str, Any]]] = {}
    dropped_bar_count = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped_bar_count += 1
            continue
        raw_time = next(
            (
                row.get(key)
                for key in (
                    "time", "trade_date", "date", "datetime", "timestamp", "index"
                )
                if row.get(key) is not None
            ),
            None,
        )
        if raw_time is None:
            dropped_bar_count += 1
            continue
        normalized_time = _normalize_time(raw_time, interval)
        if normalized_time is None:
            dropped_bar_count += 1
            continue
        time_value, sort_value = normalized_time

        open_value = _number(row.get("open"))
        high_value = _number(row.get("high"))
        low_value = _number(row.get("low"))
        close_value = _number(row.get("close"))
        volume_raw = row["volume"] if "volume" in row else 0.0
        volume_value = _number(volume_raw)
        if None in (open_value, high_value, low_value, close_value):
            dropped_bar_count += 1
            continue
        if min(open_value, high_value, low_value, close_value) <= 0:
            dropped_bar_count += 1
            continue
        if volume_value is None or volume_value < 0:
            dropped_bar_count += 1
            continue
        if (
            high_value < max(open_value, close_value, low_value)
            or low_value > min(open_value, close_value, high_value)
        ):
            dropped_bar_count += 1
            continue

        if time_value in bars_by_time:
            dropped_bar_count += 1
        bars_by_time[time_value] = (
            sort_value,
            {
                "time": time_value,
                "open": open_value,
                "high": high_value,
                "low": low_value,
                "close": close_value,
                "volume": volume_value,
            },
        )

    ordered = sorted(bars_by_time.values(), key=lambda item: item[0])
    bars = [bar for _, bar in ordered]
    truncated = len(bars) > _MAX_BARS
    return bars[-_MAX_BARS:], truncated, dropped_bar_count


def _date_range(start_date: Any, end_date: Any, interval: str) -> tuple[str, str]:
    today = date.today()
    end = date.fromisoformat(str(end_date)) if end_date else today
    start = (
        date.fromisoformat(str(start_date))
        if start_date
        else end - timedelta(days=_DEFAULT_LOOKBACK_DAYS[interval])
    )
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    return start.isoformat(), end.isoformat()


def _effective_fetch_start(
    symbol: str,
    source: str,
    interval: str,
    requested_start: str,
    end_date: str,
) -> str:
    """Bound an intraday provider request before it can materialize years of bars."""
    if interval not in _INTRADAY_INTERVALS:
        return requested_start

    market = _market_for(symbol)
    minutes_per_bar = _INTERVAL_MINUTES[interval]
    session_minutes = _SESSION_MINUTES.get(market, _SESSION_MINUTES["Unknown"])
    bars_per_session = max(1, math.ceil(session_minutes / minutes_per_bar))
    sessions_needed = max(1, math.ceil(_MAX_BARS / bars_per_session))

    if market in {"Crypto", "Unknown"}:
        span_days = sessions_needed - 1
    else:
        # Convert required exchange sessions to calendar days and allow a
        # small holiday cushion. The final 5,000-bar slice remains defensive.
        span_days = math.ceil(sessions_needed * 7 / 5) + 3

    provider_cap = _PROVIDER_MAX_INTRADAY_SPAN_DAYS.get(source, {}).get(interval)
    if provider_cap is not None:
        span_days = min(span_days, provider_cap)

    requested = date.fromisoformat(requested_start)
    end = date.fromisoformat(end_date)
    budget_start = end - timedelta(days=max(0, span_days))
    return max(requested, budget_start).isoformat()


def _preferred_source(symbol: str, interval: str, start_date: str, end_date: str) -> str:
    if symbol.endswith(".BJ"):
        return "eastmoney"
    if symbol.endswith((".SH", ".SZ")):
        if interval in _INTRADAY_INTERVALS:
            return "eastmoney"
        # Tencent requests are hard-capped at 500 daily bars. Route longer
        # windows to Eastmoney so a multi-year chart is not silently shortened.
        span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
        if span_days > 700:
            return "eastmoney"
    return detect_source(symbol)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


class PriceChartTool(BaseTool):
    """Fetch OHLCV data and attach an interactive chart to the chat answer."""

    name = "show_price_chart"
    description = (
        "Show interactive candlestick/K-line charts directly inside the chat. "
        "Use this whenever the user asks to view, inspect, compare, or display a "
        "stock/ETF/index/crypto price chart or K-line. Supports up to five symbols "
        "and persists the chart so it remains visible in conversation history."
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": _MAX_SYMBOLS,
                "description": (
                    'One to five symbols, for example ["600519.SH"], '
                    '["AAPL.US", "700.HK"], or ["^GSPC"]. Bare six-digit A-share codes are accepted.'
                ),
            },
            "start_date": {
                "type": "string",
                "description": "Optional start date (YYYY-MM-DD). Daily charts default to five years; intraday defaults depend on the bar interval.",
            },
            "end_date": {
                "type": "string",
                "description": "Optional end date (YYYY-MM-DD). Defaults to today.",
            },
            "source": {
                "type": "string",
                "description": "Optional market-data source. Defaults to automatic source selection with fallback.",
                "default": "auto",
            },
            "interval": {
                "type": "string",
                "enum": list(_SUPPORTED_INTERVALS),
                "description": "Bar interval: 1m, 5m, 15m, 30m, 1H, or 1D.",
                "default": "1D",
            },
        },
        "required": ["codes"],
    }
    repeatable = True
    is_readonly = False
    requires_current_run_dir = True

    def execute(self, **kwargs: Any) -> str:
        raw_codes = kwargs.get("codes")
        if not isinstance(raw_codes, list):
            raise ValueError("codes must be an array")
        codes = list(dict.fromkeys(_normalize_symbol(code) for code in raw_codes))
        codes = [code for code in codes if code]
        if not codes:
            raise ValueError("at least one symbol is required")
        if len(codes) > _MAX_SYMBOLS:
            raise ValueError(f"at most {_MAX_SYMBOLS} symbols can be charted at once")
        unsupported_indices = [
            code
            for code in codes
            if code.startswith("^") and code not in YAHOO_INDEX_SYMBOLS
        ]
        if unsupported_indices:
            supported = ", ".join(sorted(YAHOO_INDEX_SYMBOLS))
            raise ValueError(
                f"unsupported index symbol(s): {', '.join(unsupported_indices)}; supported Yahoo indices: {supported}"
            )

        interval = _normalize_interval(kwargs.get("interval"))
        start_date, end_date = _date_range(
            kwargs.get("start_date"), kwargs.get("end_date"), interval
        )

        run_dir_raw = str(kwargs.get("run_dir") or "").strip()
        if not run_dir_raw:
            raise ValueError("run_dir is required")
        run_dir = safe_run_dir(run_dir_raw)
        output_dir = safe_path("artifacts/visualizations", run_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        requested_source = str(kwargs.get("source") or "auto").strip().lower()
        resolved_sources: dict[str, str] = {}

        def resolve_loader(source: str) -> type:
            loader_cls = get_loader(source)
            try:
                resolved_sources[source] = str(getattr(loader_cls(), "name", source))
            except Exception:
                resolved_sources[source] = source
            return loader_cls

        groups: dict[tuple[str, str], list[str]] = {}
        source_by_symbol: dict[str, str] = {}
        fetch_start_by_symbol: dict[str, str] = {}
        for symbol in codes:
            source = (
                requested_source
                if requested_source != "auto"
                else _preferred_source(symbol, interval, start_date, end_date)
            )
            fetch_start = _effective_fetch_start(
                symbol, source, interval, start_date, end_date
            )
            groups.setdefault((source, fetch_start), []).append(symbol)
            source_by_symbol[symbol] = source
            fetch_start_by_symbol[symbol] = fetch_start

        data: dict[str, Any] = {}
        unresolved: set[str] = set()
        for (source, fetch_start), source_codes in groups.items():
            source_data = fetch_market_data(
                codes=source_codes,
                start_date=fetch_start,
                end_date=end_date,
                source=source,
                interval=interval,
                max_rows=0,
                loader_resolver=resolve_loader,
            )
            if isinstance(source_data, dict):
                unresolved.update(source_data.get("_unresolved", []))
                data.update({key: value for key, value in source_data.items() if key != "_unresolved"})
            else:
                unresolved.update(source_codes)

        manifest_path = safe_path(f"artifacts/{_MANIFEST_NAME}", run_dir)
        manifest: list[dict[str, Any]] = []
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                manifest = [item for item in existing if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            pass

        created: list[dict[str, Any]] = []
        for symbol in codes:
            bars, row_truncated, dropped_bar_count = _normalize_bars(data.get(symbol), interval)
            if not bars:
                unresolved.add(symbol)
                continue

            preferred_source = source_by_symbol[symbol]
            actual_source = resolved_sources.get(preferred_source, preferred_source)
            effective_fetch_start = fetch_start_by_symbol[symbol]
            range_truncated = effective_fetch_start > start_date
            truncated = row_truncated or range_truncated
            identity = f"{symbol}|{interval}|{start_date}|{end_date}|{actual_source}"
            visualization_id = "kline_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            fetched_at = datetime.now(timezone.utc).isoformat()
            payload = {
                "schema_version": 1,
                "visualization_id": visualization_id,
                "type": "candlestick_volume",
                "symbol": symbol,
                "market": _market_for(symbol),
                "timeframe": interval,
                "source": actual_source,
                "adjustment": _adjustment_for(actual_source),
                "timezone": _timezone_for(symbol, actual_source),
                "requested_start": start_date,
                "requested_end": end_date,
                "effective_fetch_start": effective_fetch_start,
                "effective_fetch_end": end_date,
                "retention_policy": _RETENTION_POLICY,
                "actual_start": bars[0]["time"],
                "actual_end": bars[-1]["time"],
                "fetched_at": fetched_at,
                "truncated": truncated,
                "dropped_bar_count": dropped_bar_count,
                "bars": bars,
            }
            _write_json(output_dir / f"{visualization_id}.json", payload)

            spec = {
                key: payload[key]
                for key in (
                    "schema_version", "visualization_id", "type", "symbol", "market",
                    "timeframe", "source", "adjustment", "timezone", "requested_start", "requested_end",
                    "effective_fetch_start", "effective_fetch_end", "retention_policy",
                    "actual_start", "actual_end", "fetched_at",
                )
            }
            spec.update(
                {
                    "title": f"{symbol} K-line",
                    "bar_count": len(bars),
                    "dropped_bar_count": dropped_bar_count,
                    "truncated": truncated,
                    "data_ref": visualization_id,
                    "fallback_text": f"{symbol}: {len(bars)} {interval} bars ({bars[0]['time']} to {bars[-1]['time']})",
                }
            )
            manifest = [item for item in manifest if item.get("visualization_id") != visualization_id]
            manifest.append(spec)
            created.append(spec)

        if created:
            _write_json(manifest_path, manifest[-20:])

        return json.dumps(
            {
                "status": "ok" if created else "error",
                "visualizations": created,
                "unresolved": sorted(unresolved),
                "message": "Interactive K-line chart attached to the chat response." if created else "No market data was available for the requested symbols.",
            },
            ensure_ascii=False,
            allow_nan=False,
        )
