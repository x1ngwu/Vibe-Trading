"""Shared market data helpers for MCP and local agent tools."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 250
LOCAL_CANONICAL_MODE_ENV = "VIBE_LOCAL_CANONICAL_MODE"
LOCAL_CANONICAL_MODES = frozenset({"disabled", "explicit", "auto"})
_A_SHARE_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SZ|SH|BJ)$", re.I)

YAHOO_INDEX_SYMBOLS = frozenset({"^GSPC", "^IXIC", "^DJI"})
# Symbol -> preferred source. The matched source is the head of its market's
# fallback chain (registry.FALLBACK_CHAINS), so an unavailable preferred source
# still degrades gracefully to the rest of the chain. US/HK equities route to
# the throttle-tolerant Yahoo public endpoint first (lower IP-ban risk than the
# yfinance SDK), A-shares to the Tencent quote endpoint.
_SOURCE_PATTERNS = [
    (re.compile(r"^local:", re.I), "local"),
    (re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.I), "tencent"),
    (re.compile(r"^\^(?:GSPC|IXIC|DJI)$", re.I), "yahoo"),
    (re.compile(r"^[A-Z]+\.US$", re.I), "yahoo"),
    (re.compile(r"^\d{3,5}\.HK$", re.I), "yahoo"),
    # India: NSE (RELIANCE.NS) / BSE (500325.BO). Tickers may carry '&' and '-'
    # (e.g. M&M.NS, BAJAJ-AUTO.NS). Served by Yahoo's public chart endpoint.
    (re.compile(r"^[A-Z0-9&.\-]+\.(NS|BO)$", re.I), "yahoo"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt"),
]


class LocalCanonicalModeError(ValueError):
    status = "invalid_configuration"


class LocalCanonicalDisabledError(RuntimeError):
    status = "source_disabled"


def local_canonical_mode() -> str:
    """Return the fail-closed local-canonical routing mode."""
    mode = os.getenv(LOCAL_CANONICAL_MODE_ENV, "disabled").strip().lower()
    if mode not in LOCAL_CANONICAL_MODES:
        expected = ", ".join(sorted(LOCAL_CANONICAL_MODES))
        raise LocalCanonicalModeError(
            f"{LOCAL_CANONICAL_MODE_ENV} must be one of: {expected}"
        )
    return mode


def detect_source(code: str) -> str:
    """Infer the best loader source for a normalized symbol."""
    for pattern, source in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "tushare"


def get_loader(source: str):
    """Get loader class via registry with fallback support."""
    if source == "local_canonical":
        # Explicit/local-auto requests need the loader's typed integrity and
        # coverage errors. The generic registry availability probe intentionally
        # collapses those details and is therefore only suitable for network
        # fallback sources.
        from backtest.loaders.local_canonical_loader import DataLoader

        return DataLoader
    from backtest.loaders.registry import get_loader_cls_with_fallback

    return get_loader_cls_with_fallback(source)


def cap_rows(records: list, max_rows: int) -> list | dict[str, object]:
    """Bound a per-symbol row list to keep tool payloads within budget."""
    n = len(records)
    if max_rows < 0:
        max_rows = DEFAULT_MAX_ROWS
    if max_rows == 0 or n <= max_rows:
        return records
    step = math.ceil(n / max_rows)
    sampled = records[::step]
    if sampled[-1] is not records[-1]:
        sampled = sampled + [records[-1]]
    return {
        "rows": n,
        "returned": len(sampled),
        "truncated": True,
        "policy": f"every-{step}th-row (even stride; last bar pinned)",
        "hint": "narrow the date range, coarsen interval, or set max_rows=0 for all rows",
        "data": sampled,
    }


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def fetch_market_data(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
    max_rows: int = DEFAULT_MAX_ROWS,
    loader_resolver: Callable[[str], type] = get_loader,
) -> dict[str, Any]:
    """Fetch normalized OHLCV data through the repository loader layer."""
    results: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    source_errors: dict[str, dict[str, str]] = {}
    routing: dict[str, dict[str, Any]] = {}

    def load_group(
        requested: str,
        group_codes: list[str],
    ) -> tuple[dict[str, Any], str, Exception | None]:
        try:
            loader_cls = loader_resolver(requested)
            loader = loader_cls()
            actual = str(getattr(loader, "name", requested))
            data_map = loader.fetch(
                group_codes, start_date, end_date, interval=interval
            )
            return data_map, actual, None
        except Exception as exc:
            logger.exception(
                "market-data loader %r failed for %s",
                requested,
                group_codes,
            )
            return {}, requested, exc

    def consume(
        data_map: dict[str, Any],
        *,
        requested: str,
        actual: str,
        group_codes: list[str],
        fallback_reason_by_code: dict[str, str],
    ) -> set[str]:
        resolved: set[str] = set()
        for symbol, df in data_map.items():
            if symbol not in group_codes:
                continue
            frame_provenance = getattr(df, "attrs", {}).get("provenance")
            if isinstance(frame_provenance, dict):
                provenance[symbol] = frame_provenance
            records = df.reset_index().to_dict(orient="records")
            for row in records:
                for key, value in row.items():
                    row[key] = _json_safe(value)
            results[symbol] = cap_rows(records, max_rows)
            reason = fallback_reason_by_code.get(symbol)
            routing[symbol] = {
                "requested_source": source,
                "preferred_source": requested,
                "actual_source": actual,
                "fallback": bool(reason or actual != requested),
            }
            if reason:
                routing[symbol]["fallback_reason"] = reason
            elif actual != requested:
                routing[symbol]["fallback_reason"] = "preferred_source_unavailable"
            resolved.add(symbol)
        return resolved

    if source in {"auto", "local_canonical"}:
        try:
            mode = local_canonical_mode()
        except LocalCanonicalModeError as exc:
            source_errors["local_canonical"] = {
                "status": exc.status,
                "detail": str(exc),
            }
            mode = "invalid"
    else:
        mode = "disabled"

    if mode == "invalid":
        for code in codes:
            routing[code] = {
                "requested_source": source,
                "actual_source": "local_canonical",
                "fallback": False,
                "status": "invalid_configuration",
            }
    elif source == "local_canonical" and mode == "disabled":
        exc = LocalCanonicalDisabledError(
            "local canonical source is disabled by VIBE_LOCAL_CANONICAL_MODE"
        )
        source_errors["local_canonical"] = {
            "status": exc.status,
            "detail": str(exc),
        }
        for code in codes:
            routing[code] = {
                "requested_source": source,
                "actual_source": "local_canonical",
                "fallback": False,
                "status": exc.status,
            }
    else:
        groups: dict[str, list[str]] = {}
        fallback_reason_by_code: dict[str, str] = {}
        local_codes: list[str] = []
        if source == "auto":
            for code in codes:
                if mode == "auto" and interval == "1D" and _A_SHARE_SYMBOL_RE.fullmatch(code):
                    local_codes.append(code)
                else:
                    groups.setdefault(detect_source(code), []).append(code)
        else:
            groups = {source: list(codes)}

        if local_codes:
            if len(local_codes) > 128:
                source_errors["local_canonical"] = {
                    "status": "invalid_request",
                    "detail": "local canonical auto routing supports at most 128 symbols",
                }
                for code in local_codes:
                    routing[code] = {
                        "requested_source": source,
                        "actual_source": "local_canonical",
                        "fallback": False,
                        "status": "invalid_request",
                    }
            else:
                local_map, actual, local_error = load_group(
                    "local_canonical", local_codes
                )
                if local_error is not None:
                    status = str(
                        getattr(local_error, "status", "source_unavailable")
                    )
                    if status == "incomplete":
                        for code in local_codes:
                            preferred = detect_source(code)
                            groups.setdefault(preferred, []).append(code)
                            fallback_reason_by_code[code] = "local_coverage_incomplete"
                    else:
                        source_errors["local_canonical"] = {
                            "status": status,
                            "detail": str(local_error),
                        }
                        for code in local_codes:
                            routing[code] = {
                                "requested_source": source,
                                "actual_source": "local_canonical",
                                "fallback": False,
                                "status": status,
                            }
                else:
                    local_resolved = consume(
                        local_map,
                        requested="local_canonical",
                        actual=actual,
                        group_codes=local_codes,
                        fallback_reason_by_code=fallback_reason_by_code,
                    )
                    for code in local_codes:
                        if code not in local_resolved:
                            preferred = detect_source(code)
                            groups.setdefault(preferred, []).append(code)
                            fallback_reason_by_code[code] = "local_no_data"

        for requested, group_codes in groups.items():
            data_map, actual, error = load_group(requested, group_codes)
            if error is not None:
                if requested == "local_canonical":
                    source_errors[requested] = {
                        "status": str(
                            getattr(error, "status", "source_unavailable")
                        ),
                        "detail": str(error),
                    }
                for code in group_codes:
                    reason = fallback_reason_by_code.get(code)
                    routing.setdefault(
                        code,
                        {
                            "requested_source": source,
                            "preferred_source": requested,
                            "actual_source": actual,
                            "fallback": bool(reason or actual != requested),
                            "status": str(
                                getattr(error, "status", "source_unavailable")
                            ),
                        },
                    )
                    if reason:
                        routing[code]["fallback_reason"] = reason
                continue
            resolved = consume(
                data_map,
                requested=requested,
                actual=actual,
                group_codes=group_codes,
                fallback_reason_by_code=fallback_reason_by_code,
            )
            for code in group_codes:
                if code in resolved:
                    continue
                reason = fallback_reason_by_code.get(code)
                routing.setdefault(
                    code,
                    {
                        "requested_source": source,
                        "preferred_source": requested,
                        "actual_source": actual,
                        "fallback": bool(reason or actual != requested),
                        "status": "no_data",
                    },
                )
                if reason:
                    routing[code]["fallback_reason"] = reason

    unresolved = [code for code in codes if code not in results]
    if unresolved:
        results["_unresolved"] = unresolved
    if provenance:
        results["_provenance"] = provenance
    if source_errors:
        results["_errors"] = source_errors
    if routing:
        results["_routing"] = routing

    return results


def fetch_market_data_json(**kwargs: Any) -> str:
    """Fetch market data and return strict JSON."""
    return json.dumps(fetch_market_data(**kwargs), ensure_ascii=False, indent=2, allow_nan=False)
