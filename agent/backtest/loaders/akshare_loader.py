"""AKShare loader: free, no-auth data for A-shares, US, HK, futures, forex, macro.

AKShare (https://github.com/akfamily/akshare) is a completely free financial
data aggregator covering Chinese and global markets.  No API token required.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders._symbol_utils import _is_etf_listed
from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.data_envelope import LoaderCapability
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_INTERVAL_MAP_DAILY = {
    "1D": "daily",
    "1W": "weekly",
    "1M": "monthly",
}

# ``stock_zh_a_hist(adjust="")`` returns unadjusted A-share daily bars. Its
# volume is lots (手) and amount is CNY; the strict adapter converts amount to
# CNY thousands so fallback units match the Tushare raw contract.
QE2_A_SHARE_DAILY_RAW_CAPABILITY = LoaderCapability(
    source="akshare",
    version="akshare-a-share-daily-raw-v1",
    instrument_types=("stock",),
    intervals=("1D",),
    adjustments=("raw",),
    fields=("open", "high", "low", "close", "volume", "amount"),
    field_units={
        "stock": {
            "open": "CNY/share", "high": "CNY/share",
            "low": "CNY/share", "close": "CNY/share",
            "volume": "lot_100_shares", "amount": "CNY_1000",
        },
    },
)


def _is_a_share(code: str) -> bool:
    return code.upper().endswith((".SZ", ".SH", ".BJ"))


def _is_hk(code: str) -> bool:
    return code.upper().endswith(".HK")


def _is_us(code: str) -> bool:
    return code.upper().endswith(".US")


def _is_crypto(code: str) -> bool:
    return "-USDT" in code.upper() or "/USDT" in code.upper()





def _is_forex(code: str) -> bool:
    """Detect forex pairs by matching against AKShare's symbol_market_map.

    Issue #54 — forex symbols (EURUSD, GBPUSD, etc.) have no exchange suffix
    and previously fell through to the A-share endpoint.
    """
    upper = code.upper().removesuffix(".FX")
    try:
        from akshare.forex.cons import symbol_market_map
    except Exception:
        return False
    return upper in symbol_market_map


@register
class DataLoader:
    """AKShare universal OHLCV loader (free, no auth)."""

    name = "akshare"
    markets = {"a_share", "us_equity", "hk_equity", "futures", "fund", "macro", "forex"}
    requires_auth = False

    def is_available(self) -> bool:
        """Available if akshare is installed."""
        try:
            import akshare  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self) -> None:
        pass

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV data via AKShare.

        Args:
            codes: Symbol list.
            start_date: YYYY-MM-DD.
            end_date: YYYY-MM-DD.
            interval: Bar size (only 1D supported currently).
            fields: Ignored.

        Returns:
            Mapping symbol -> OHLCV DataFrame.
        """
        validate_date_range(start_date, end_date)

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(code, start_date, end_date, interval),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:
                logger.warning("akshare failed for %s: %s", code, exc)
        return result


    def fetch_for_envelope(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Strict raw A-share fetch; provider exceptions remain visible."""

        validate_date_range(start_date, end_date)
        if interval != "1D":
            raise ValueError("QE2 AKShare capability only supports interval='1D'")
        if fields:
            raise ValueError("QE2 strict daily fetch does not accept extra fields")

        import akshare as ak

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            if not _is_a_share(code) or _is_etf_listed(code):
                raise ValueError(f"QE2 AKShare raw capability does not support {code}")
            frame = cached_loader_fetch(
                source=self.name,
                symbol=code,
                timeframe="1D",
                start_date=start_date,
                end_date=end_date,
                fields=["__qe2_raw_ohlcva_v1"],
                fetch=lambda code=code: self._fetch_a_share_raw(
                    ak, code, start_date, end_date
                ),
            )
            if frame is not None and not frame.empty:
                result[code] = frame
        return result

    def _fetch_a_share_raw(
        self, ak, code: str, start_date: str, end_date: str,
    ) -> Optional[pd.DataFrame]:
        df = ak.stock_zh_a_hist(
            symbol=code.split(".")[0],
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="",
        )
        if df is None or df.empty:
            return None
        normalized = self._normalize(df, date_col="日期", include_amount=True)
        normalized["amount"] = normalized["amount"] / 1000.0
        return normalized
    def _fetch_one(
        self, code: str, start_date: str, end_date: str, interval: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch a single symbol."""
        import akshare as ak

        # ETF check must precede A-share — 518880.SH ends with .SH but is an ETF.
        if _is_etf_listed(code):
            return self._fetch_etf(ak, code, start_date, end_date)
        if _is_a_share(code):
            return self._fetch_a_share(ak, code, start_date, end_date, interval)
        if _is_us(code):
            return self._fetch_us(ak, code, start_date, end_date)
        if _is_hk(code):
            return self._fetch_hk(ak, code, start_date, end_date)
        if _is_forex(code):
            return self._fetch_forex(ak, code, start_date, end_date)
        # Default: try A-share
        return self._fetch_a_share(ak, code, start_date, end_date, interval)

    def _fetch_a_share(
        self, ak, code: str, start_date: str, end_date: str, interval: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch A-share via stock_zh_a_hist."""
        symbol = code.split(".")[0]
        period = _INTERVAL_MAP_DAILY.get(interval, "daily")
        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period=period,
            start_date=sd,
            end_date=ed,
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        return self._normalize(df, date_col="日期")

    def _fetch_us(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch US stock via stock_us_hist."""
        symbol = code.replace(".US", "")
        # akshare uses the format like "105.AAPL" for NASDAQ
        # Try common prefixes
        for prefix in ["105.", "106.", ""]:
            try:
                df = ak.stock_us_hist(
                    symbol=f"{prefix}{symbol}",
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
                if df is not None and not df.empty:
                    return self._normalize(df, date_col="日期")
            except Exception:
                continue
        return None

    def _fetch_etf(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch exchange-listed ETF / LOF via fund_etf_hist_sina.

        Sina symbol format is ``sh518880`` / ``sz159915``. The endpoint returns
        the full history; we filter to the requested window after fetching.
        """
        digits, _, suffix = code.upper().partition(".")
        symbol = f"{suffix.lower()}{digits}"
        df = ak.fund_etf_hist_sina(symbol=symbol)
        if df is None or df.empty:
            return None
        df = self._normalize(df, date_col="date")
        # fund_etf_hist_sina returns full history — clip to window.
        return df.loc[start_date:end_date]

    def _fetch_forex(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch forex pair via forex_hist_em.

        Columns returned are 日期 / 代码 / 名称 / 今开 / 最新价 / 最高 / 最低 / 振幅
        — note ``最新价`` (latest) plays the role of close. Volume isn't reported,
        so we synthesize a zero column to satisfy the OHLCV contract.
        """
        symbol = code.upper().removesuffix(".FX")
        df = ak.forex_hist_em(symbol=symbol)
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "日期": "trade_date",
            "今开": "open",
            "最新价": "close",
            "最高": "high",
            "最低": "low",
        })
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        df["volume"] = 0.0
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        return df.loc[start_date:end_date]

    def _fetch_hk(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch HK stock via stock_hk_hist."""
        symbol = code.replace(".HK", "").zfill(5)
        df = ak.stock_hk_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        return self._normalize(df, date_col="日期")

    @staticmethod
    def _normalize(
        df: pd.DataFrame, date_col: str = "日期", *, include_amount: bool = False,
    ) -> pd.DataFrame:
        """Normalize AKShare DataFrame to standard OHLCV schema.

        AKShare Chinese column names: 日期, 开盘, 最高, 最低, 收盘, 成交量
        AKShare English column names: date, open, high, low, close, volume
        """
        col_map_cn = {"开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"}
        col_map_en = {"date": "trade_date", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}

        if date_col in df.columns:
            df = df.rename(columns={date_col: "trade_date"})
        elif "date" in df.columns:
            df = df.rename(columns={"date": "trade_date"})

        # Try Chinese column names first, then English
        if "开盘" in df.columns:
            df = df.rename(columns=col_map_cn)
        else:
            df = df.rename(columns=col_map_en)

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()

        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        fields = ["open", "high", "low", "close", "volume"]
        if include_amount:
            if "amount" not in df.columns:
                raise ValueError("AKShare raw A-share response omitted amount")
            fields.append("amount")
        ohlcv_cols = [c for c in fields if c in df.columns]
        df = df[ohlcv_cols].dropna(subset=["open", "high", "low", "close"])
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return df
