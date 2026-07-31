"""Provider-neutral and BaoStock transitional QE5 materialization tests."""

from __future__ import annotations

import json
import stat
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from src.quant_engine import (
    BaoStockCaptureError,
    Qe5CapturedInstrument,
    Qe5UniverseInstrument,
    Qe5UniverseManifest,
    build_qe5_snapshot_payload,
    capture_baostock_market,
    materialize_qe5_capture,
)
from src.quant_engine.runner import compute_snapshot_sha256
from src.quant_engine.snapshot_materialization import SHANGHAI
from src.research.contracts import DataSnapshotRef, ResearchSpec
from src.research.store import ResearchStore

AGENT_ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = AGENT_ROOT / "engine_workers" / "quantaxis"
COMMON_DIR = AGENT_ROOT / "engine_workers" / "common"
for _path in (str(COMMON_DIR), str(WORKER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from formal_backtest import _parse_snapshot  # noqa: E402


OPEN_DATES = (
    "2020-01-02",
    "2020-01-03",
    "2020-01-06",
    "2020-01-07",
    "2020-01-08",
    "2020-01-09",
    "2025-06-03",
    "2025-06-04",
    "2025-06-05",
)


class _Result:
    error_code = "0"
    error_msg = "success"

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.fields = list(rows[0]) if rows else ["code"]
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return [self._rows[self._index][field] for field in self.fields]


class _Login:
    error_code = "0"
    error_msg = "success"


class _BaoStock:
    __version__ = "test-0.9.30"

    def __init__(
        self,
        *,
        omit_qfq_last: bool = False,
        cash_per_share: str = "0.20",
        stock_per_share: str = "0",
    ) -> None:
        self.omit_qfq_last = omit_qfq_last
        self.cash_per_share = cash_per_share
        self.stock_per_share = stock_per_share
        self.logged_out = False

    def login(self):
        return _Login()

    def logout(self):
        self.logged_out = True

    def query_stock_basic(self, *, code: str):
        return _Result(
            [
                {
                    "code": code,
                    "code_name": "fixture",
                    "ipoDate": "2020-01-02",
                    "outDate": "",
                    "type": "1",
                    "status": "1",
                }
            ]
        )

    def query_trade_dates(self, *, start_date: str, end_date: str):
        return _Result(
            [
                {"calendar_date": value, "is_trading_day": "1"}
                for value in OPEN_DATES
                if start_date <= value <= end_date
            ]
        )

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ):
        assert frequency == "d"
        rows = []
        dates = list(OPEN_DATES[-3:])
        if adjustflag == "2" and self.omit_qfq_last:
            dates.pop()
        for index, trade_date in enumerate(dates):
            raw_close = 10 + index
            rows.append(
                {
                    "date": trade_date,
                    "code": code,
                    "open": f"{raw_close}.00",
                    "high": f"{raw_close + 1}.00",
                    "low": f"{raw_close - 1}.00",
                    "close": (
                        f"{raw_close}.0049"
                        if adjustflag == "2"
                        else f"{raw_close}.00"
                    ),
                    "preclose": f"{max(9, raw_close - 1)}.00",
                    "volume": "10000",
                    "amount": "123456.789",
                    "adjustflag": adjustflag,
                    "tradestatus": "1",
                    "isST": "0",
                }
            )
        return _Result(rows)

    def query_dividend_data(self, code: str, *, year: int, yearType: str):
        if year != 2025:
            return _Result([])
        return _Result(
            [
                {
                    "code": code,
                    "dividPreNoticeDate": "",
                    "dividAgmPumDate": "2025-05-20",
                    "dividPlanAnnounceDate": "2025-03-31",
                    "dividPlanDate": "2025-05-21",
                    "dividRegistDate": "2025-06-03",
                    "dividOperateDate": "2025-06-04",
                    "dividPayDate": "2025-06-05",
                    "dividStockMarketDate": "",
                    "dividCashPsBeforeTax": self.cash_per_share,
                    "dividCashPsAfterTax": "0.18",
                    "dividStocksPs": self.stock_per_share,
                    "dividCashStock": "10派2元",
                    "dividReserveToStockPs": "0",
                }
            ]
        )


def _manifest() -> Qe5UniverseManifest:
    return Qe5UniverseManifest(
        universe_id="qe5-baostock-fixture-v1",
        instruments=(
            Qe5UniverseInstrument(symbol="600600.SH", board="sh_main"),
        ),
    )


def _capture(provider: _BaoStock | None = None):
    return capture_baostock_market(
        _manifest(),
        start_date=date(2025, 6, 3),
        end_date=date(2025, 6, 5),
        as_of=date(2025, 6, 5),
        provider_module=provider or _BaoStock(),
        captured_at=datetime(2025, 6, 6, 9, 0, tzinfo=SHANGHAI),
    )


def test_baostock_capture_is_provider_neutral_and_point_in_time() -> None:
    provider = _BaoStock()
    capture = _capture(provider)

    assert provider.logged_out is True
    assert capture.provider == "baostock"
    assert capture.calendar == tuple(date.fromisoformat(value) for value in OPEN_DATES[-3:])
    assert len(capture.bars) == 3
    assert capture.bars[0].listing_trade_day_number == 7
    assert capture.bars[0].signal_close_fen == 1_000
    assert capture.bars[0].amount_fen == 12_345_679
    assert capture.corporate_actions[0].cash_per_share_numerator_fen == 20
    assert capture.corporate_actions[0].cash_per_share_denominator == 1
    assert capture.corporate_actions[0].known_at.date() == date(2025, 3, 31)
    assert "transitional_source:baostock" in capture.anomalies
    assert any("differentiated_holding_period_tax" in item for item in capture.anomalies)

    payload = build_qe5_snapshot_payload(capture)
    assert payload["price_semantics"] == {
        "execution_price_adjustment": "raw",
        "signal_price_adjustment": "qfq",
        "corporate_action_mode": "explicit",
    }
    assert payload["bars"][0]["market_rule_id"] == (
        "sh_main-standard-10pct-post-20230828-v1"
    )
    assert payload["bars"][0]["features"] == {"amount_fen": 12_345_679}


def test_baostock_capture_rejects_raw_qfq_date_mismatch_and_logs_out() -> None:
    provider = _BaoStock(omit_qfq_last=True)
    with pytest.raises(
        BaoStockCaptureError,
        match="history does not cover every requested open date",
    ):
        _capture(provider)
    assert provider.logged_out is True


def test_baostock_capture_preserves_sub_fen_dividend_as_rational() -> None:
    capture = _capture(_BaoStock(cash_per_share="5.400078"))
    action = capture.corporate_actions[0]
    assert action.cash_per_share_numerator_fen == 2_700_039
    assert action.cash_per_share_denominator == 5_000
    assert action.cash_rounding == "half_up_total_fen"


def test_rational_share_split_with_no_cash_is_worker_compatible(
    tmp_path: Path,
) -> None:
    capture = _capture(
        _BaoStock(cash_per_share="0", stock_per_share="0.25")
    )
    action = capture.corporate_actions[0]
    assert action.kind == "share_split"
    assert action.multiplier_numerator == 5
    assert action.multiplier_denominator == 4
    assert action.cash_rounding == "none"

    result = materialize_qe5_capture(capture, tmp_path / "share-split")
    parsed = _parse_snapshot(
        {
            "path": str(result.snapshot_path),
            "sha256": result.manifest.snapshot_sha256,
        }
    )
    assert parsed["actions"][0]["kind"] == "share_split"
    assert parsed["actions"][0]["cash_per_share_numerator_fen"] == 0
    assert parsed["actions"][0]["cash_per_share_denominator"] == 1
    assert parsed["actions"][0]["cash_rounding"] == "none"


def test_universe_requires_explicit_exchange_consistent_main_board() -> None:
    with pytest.raises(ValueError, match="sh_main instrument"):
        Qe5UniverseManifest(
            universe_id="bad-board",
            instruments=(
                Qe5UniverseInstrument(symbol="000001.SZ", board="sh_main"),
            ),
        )
    with pytest.raises(ValueError, match="outside the explicit main-board"):
        Qe5UniverseInstrument(symbol="688001.SH", board="sh_main")
    with pytest.raises(ValueError, match="outside the explicit main-board"):
        Qe5UniverseInstrument(symbol="300001.SZ", board="sz_main")
    with pytest.raises(ValueError, match="outside the explicit main-board"):
        Qe5CapturedInstrument(
            symbol="688001.SH",
            board="sh_main",
            listing_date=date(2020, 1, 2),
        )
    with pytest.raises(BaoStockCaptureError, match="2023-08-28"):
        capture_baostock_market(
            _manifest(),
            start_date=date(2023, 8, 25),
            end_date=date(2023, 8, 29),
            as_of=date(2023, 8, 29),
            provider_module=_BaoStock(),
            captured_at=datetime(2025, 6, 6, 9, 0, tzinfo=SHANGHAI),
        )


def test_materialization_bundle_and_optional_publish_are_content_bound(
    tmp_path: Path,
) -> None:
    capture = _capture()
    output = tmp_path / "audit-bundle"
    publish_root = tmp_path / "vibe-home"
    result = materialize_qe5_capture(
        capture,
        output,
        publish_root=publish_root,
    )

    assert result.manifest.capture_sha256 == capture.content_sha256
    assert compute_snapshot_sha256(result.snapshot_path) == (
        result.manifest.snapshot_sha256
    )
    parsed = _parse_snapshot(
        {
            "path": str(result.snapshot_path),
            "sha256": result.manifest.snapshot_sha256,
        }
    )
    assert parsed["snapshot_sha256"] == result.manifest.snapshot_sha256
    assert tuple(parsed["instruments"]) == ("600600.SH",)
    assert parsed["actions"][0]["cash_per_share_fen"] is None
    assert parsed["actions"][0]["cash_per_share_numerator_fen"] == 20
    assert parsed["actions"][0]["cash_per_share_denominator"] == 1
    assert parsed["actions"][0]["cash_rounding"] == "half_up_total_fen"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in (
        "capture.json",
        "snapshot.json",
        "research-spec.json",
        "data-snapshot-ref.json",
        "manifest.json",
    ):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    published = (
        publish_root
        / "backtest-snapshots"
        / f"{result.manifest.snapshot_sha256}.json"
    )
    assert published.read_bytes() == result.snapshot_path.read_bytes()

    store = ResearchStore(publish_root / "research")
    research = store.get(result.manifest.research_spec_id)
    snapshot = store.get(result.manifest.data_snapshot_id)
    assert isinstance(research.payload, ResearchSpec)
    assert isinstance(snapshot.payload, DataSnapshotRef)
    assert snapshot.payload.snapshot_sha256 == result.manifest.snapshot_sha256
    assert snapshot.payload.actual_sources == {"600600.SH": "baostock-test-0.9.30"}
    assert json.loads((output / "manifest.json").read_text())[
        "data_snapshot_id"
    ] == result.manifest.data_snapshot_id

    replay = materialize_qe5_capture(
        capture,
        tmp_path / "audit-bundle-replay",
    )
    assert replay.manifest.snapshot_sha256 == result.manifest.snapshot_sha256
    assert replay.manifest.data_snapshot_id == result.manifest.data_snapshot_id
    with pytest.raises(ValueError, match="refusing to overwrite"):
        materialize_qe5_capture(capture, output)


def test_publish_failure_never_leaves_a_manifest_claiming_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_put(self: ResearchStore, obj: object) -> None:
        raise OSError("simulated research publication failure")

    monkeypatch.setattr(ResearchStore, "put", fail_put)
    output = tmp_path / "audit-bundle"
    with pytest.raises(OSError, match="simulated research publication failure"):
        materialize_qe5_capture(
            _capture(),
            output,
            publish_root=tmp_path / "vibe-home",
        )
    assert not output.exists()
