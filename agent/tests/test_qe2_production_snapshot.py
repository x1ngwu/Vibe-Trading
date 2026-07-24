"""QE2 production-runner and run-card tests for content-bound data snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.loaders.data_envelope import (
    DataFetchRequest,
    OfflineDataSnapshot,
    SourceAttempt,
    SymbolOutcome,
)
from backtest.runner import (
    BacktestConfigSchema,
    _fetch_auto,
    _load_offline_data_snapshot,
    main,
)
from src.research.contracts import canonical_json


SYMBOL = "600001.SH"
START = "2025-01-02"
END = "2025-01-06"


def _snapshot_artifact(*, close_tail: float = 10.2) -> OfflineDataSnapshot:
    request = DataFetchRequest(
        symbols=(SYMBOL,),
        instrument_types={SYMBOL: "stock"},
        start_date=START,
        end_date=END,
        adjustment="raw",
        fields=("open", "high", "low", "close", "volume", "amount"),
        requested_sources=("fixture",),
    )
    rows = (
        {"trade_date": "2025-01-02", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 1000.0, "amount": 10.1},
        {"trade_date": "2025-01-03", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 1100.0, "amount": 11.2},
        {"trade_date": "2025-01-06", "open": 10.2, "high": 10.4, "low": 10.1, "close": close_tail, "volume": 1200.0, "amount": 12.2},
    )
    return OfflineDataSnapshot(
        request=request,
        source_versions={"fixture": "qe2-production-v1"},
        actual_sources={SYMBOL: "fixture"},
        units={SYMBOL: {"open": "CNY", "high": "CNY", "low": "CNY", "close": "CNY", "volume": "share", "amount": "CNY_1000"}},
        outcomes=(
            SymbolOutcome(
                symbol=SYMBOL,
                status="ok",
                attempted_sources=("fixture",),
                actual_source="fixture",
                row_count=3,
            ),
        ),
        anomalies=(),
        source_attempts=(
            SourceAttempt(
                symbol=SYMBOL,
                source="fixture",
                status="selected",
                detail="selected 3 frozen rows",
            ),
        ),
        bars={SYMBOL: rows},
    )


def _write_snapshot(run_dir: Path, *, close_tail: float = 10.2) -> tuple[Path, str]:
    artifact = _snapshot_artifact(close_tail=close_tail)
    path = run_dir / "data" / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(artifact).encode("utf-8")
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert digest == artifact.snapshot_sha256
    return path, digest


def _config(snapshot_sha256: str) -> dict:
    return {
        "codes": [SYMBOL],
        "start_date": START,
        "end_date": END,
        "source": "tushare",
        "interval": "1D",
        "engine": "daily",
        "data_contract": "qe2_snapshot",
        "adjustment": "raw",
        "initial_cash": 100000,
        "data_snapshot": {"path": "data/snapshot.json", "sha256": snapshot_sha256},
    }


def test_snapshot_config_is_exact_and_disables_online_enrichment() -> None:
    valid = _config("a" * 64)
    assert BacktestConfigSchema(**valid).adjustment == "raw"

    with pytest.raises(ValueError, match="requires data_snapshot"):
        BacktestConfigSchema(**{key: value for key, value in valid.items() if key != "data_snapshot"})
    with pytest.raises(ValueError, match="data_contract=qe2_snapshot"):
        BacktestConfigSchema(**{**valid, "data_contract": "legacy"})
    with pytest.raises(ValueError, match="explicit adjustment"):
        BacktestConfigSchema(**{**valid, "adjustment": None})
    with pytest.raises(ValueError, match="extra_forbidden"):
        BacktestConfigSchema(**{**valid, "data_snapshot": {**valid["data_snapshot"], "url": "https://x"}})
    with pytest.raises(ValueError, match="online fundamental/event enrichment"):
        BacktestConfigSchema(**{**valid, "fundamental_fields": {"income": ["revenue"]}})
    with pytest.raises(ValueError, match="external benchmark"):
        BacktestConfigSchema(**{**valid, "benchmark": "000300.SH"})


def test_snapshot_loader_binds_request_and_returns_complete_provenance(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _, digest = _write_snapshot(run_dir)

    envelope, provenance = _load_offline_data_snapshot(run_dir, _config(digest))

    assert list(envelope.frames) == [SYMBOL]
    assert provenance == {
        "schema_version": "vibe.run-data-provenance.v1",
        "snapshot_sha256": digest,
        "request_sha256": envelope.request.request_sha256,
        "adjustment": "raw",
        "interval": "1D",
        "start_date": START,
        "end_date": END,
        "requested_sources": ["fixture"],
        "actual_sources": {SYMBOL: "fixture"},
        "source_versions": {"fixture": "qe2-production-v1"},
        "units": {SYMBOL: {"amount": "CNY_1000", "close": "CNY", "high": "CNY", "low": "CNY", "open": "CNY", "volume": "share"}},
        "availability_context_sha256": None,
    }

    with pytest.raises(ValueError, match="symbols/order"):
        _load_offline_data_snapshot(run_dir, {**_config(digest), "codes": ["600002.SH"]})
    with pytest.raises(ValueError, match="adjustment"):
        _load_offline_data_snapshot(run_dir, {**_config(digest), "adjustment": "qfq"})
    with pytest.raises(ValueError, match="content does not match"):
        _load_offline_data_snapshot(run_dir, {**_config(digest), "data_snapshot": {"path": "data/snapshot.json", "sha256": "0" * 64}})


def test_snapshot_path_must_remain_inside_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.json"
    artifact = _snapshot_artifact()
    outside.write_text(canonical_json(artifact), encoding="utf-8")
    config = _config(artifact.snapshot_sha256)
    config["data_snapshot"] = {"path": "../outside.json", "sha256": artifact.snapshot_sha256}

    with pytest.raises(ValueError, match="stay inside"):
        _load_offline_data_snapshot(run_dir, config)

    link = run_dir / "data"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    config["data_snapshot"] = {"path": "data/outside.json", "sha256": artifact.snapshot_sha256}
    with pytest.raises(ValueError, match="symlink"):
        _load_offline_data_snapshot(run_dir, config)


def test_production_runner_uses_snapshot_without_constructing_provider_and_writes_run_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "code").mkdir(parents=True)
    _, digest = _write_snapshot(run_dir)
    (run_dir / "config.json").write_text(json.dumps(_config(digest)), encoding="utf-8")
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n"
        "    def generate(self, data_map):\n"
        "        return {code: frame['close'] * 0.0 for code, frame in data_map.items()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))

    def provider_forbidden(*args, **kwargs):
        raise AssertionError("snapshot run attempted to construct/call a provider")

    monkeypatch.setattr("backtest.runner._get_loader", provider_forbidden)
    monkeypatch.setattr("backtest.runner._fetch_auto", provider_forbidden)

    main(run_dir)

    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    assert card["data_sources"] == ["fixture"]
    assert card["data_provenance"]["snapshot_sha256"] == digest
    assert card["data_provenance"]["actual_sources"] == {SYMBOL: "fixture"}
    assert card["data_provenance"]["adjustment"] == "raw"
    markdown = (run_dir / "run_card.md").read_text(encoding="utf-8")
    assert digest in markdown
    assert "actual_source[600001.SH]: `fixture`" in markdown


def test_auto_fallback_records_the_source_that_actually_returned_each_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1000.0],
        },
        index=pd.DatetimeIndex([START], name="trade_date"),
    )

    class EmptyPrimary:
        name = "tencent"

        def fetch(self, *args, **kwargs):
            return {}

    class SuccessfulFallback:
        name = "akshare"

        def is_available(self):
            return True

        def fetch(self, codes, *args, **kwargs):
            return {codes[0]: frame.copy()}

    config = {"start_date": START, "end_date": END}
    monkeypatch.setattr("backtest.runner.resolve_loader", lambda market: EmptyPrimary())
    monkeypatch.setattr(
        "backtest.runner.FALLBACK_CHAINS", {"a_share": ["tencent", "akshare"]}
    )
    monkeypatch.setattr(
        "backtest.runner.LOADER_REGISTRY", {"akshare": SuccessfulFallback}
    )

    result = _fetch_auto([SYMBOL], config)

    assert list(result) == [SYMBOL]
    assert config["_run_card_actual_sources"] == {SYMBOL: "akshare"}


def test_production_as_of_replay_ignores_future_provider_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))

    def provider_forbidden(*args, **kwargs):
        raise AssertionError("as-of replay attempted provider access")

    monkeypatch.setattr("backtest.runner._get_loader", provider_forbidden)
    monkeypatch.setattr("backtest.runner._fetch_auto", provider_forbidden)

    cards = []
    equity_artifacts = []
    for name, future_value in (("before", 99.0), ("after", 0.01)):
        # This file represents mutable provider state strictly after END.  It is
        # deliberately outside the content-bound historical snapshot.
        (tmp_path / "future-provider-state.json").write_text(
            json.dumps({"trade_date": "2025-01-07", "close": future_value}),
            encoding="utf-8",
        )
        run_dir = tmp_path / name
        (run_dir / "code").mkdir(parents=True)
        _, digest = _write_snapshot(run_dir)
        (run_dir / "config.json").write_text(json.dumps(_config(digest)), encoding="utf-8")
        (run_dir / "code" / "signal_engine.py").write_text(
            "class SignalEngine:\n"
            "    def generate(self, data_map):\n"
            "        return {code: frame['close'] * 0.0 for code, frame in data_map.items()}\n",
            encoding="utf-8",
        )
        main(run_dir)
        cards.append(json.loads((run_dir / "run_card.json").read_text(encoding="utf-8")))
        equity_artifacts.append((run_dir / "artifacts" / "equity.csv").read_bytes())

    assert cards[0]["data_provenance"] == cards[1]["data_provenance"]
    assert cards[0]["metrics"] == cards[1]["metrics"]
    assert equity_artifacts[0] == equity_artifacts[1]


def test_selected_snapshot_revision_changes_identity() -> None:
    first = _snapshot_artifact(close_tail=10.2)
    replay = _snapshot_artifact(close_tail=10.2)
    selected_revision = _snapshot_artifact(close_tail=10.3)

    assert first.snapshot_sha256 == replay.snapshot_sha256
    assert first.snapshot_sha256 != selected_revision.snapshot_sha256
