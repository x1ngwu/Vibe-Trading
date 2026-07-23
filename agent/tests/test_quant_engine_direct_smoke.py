"""Opt-in QE0 direct integration tests for the two isolated environments."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.quant_engine import EngineIdentity, WorkerConfig, WorkerRunner


AGENT_ROOT = Path(__file__).resolve().parents[1]
COMMON_RUNTIME = (AGENT_ROOT / "engine_workers" / "common").resolve()


def _runner(*, env_name: str, engine: str, commit: str, tmp_path: Path) -> WorkerRunner:
    python_text = os.environ.get(env_name)
    if not python_text:
        pytest.skip(f"set {env_name} to run the isolated direct smoke")
    python = Path(python_text).absolute()
    return WorkerRunner(
        WorkerConfig(
            engine=EngineIdentity(engine, commit),
            python=python,
            script=(AGENT_ROOT / "engine_workers" / engine / "worker.py").resolve(),
            snapshot_root=tmp_path.resolve(),
        ),
        common_runtime=COMMON_RUNTIME,
    )


def _assert_isolation(runner: WorkerRunner, engine: str) -> None:
    capabilities = runner.run(
        request_id=f"qe0-{engine}-capabilities",
        operation="capabilities",
    ).response["result"]
    assert capabilities["engine_commit"] == runner.config.engine.commit
    assert capabilities["protocol"] == {
        "name": "vibe.quant-engine.jsonl",
        "schema_version": "1.0",
    }

    probe = runner.run(
        request_id=f"qe0-{engine}-security",
        operation="security_probe",
    ).response["result"]
    assert probe["network_blocked"] is True
    assert probe["name_resolution_blocked"] is True
    assert probe["secret_named_environment"] == []
    assert probe["timezone"] == "Asia/Shanghai"
    assert probe["bind_blocked"] is True
    assert probe["datagram_blocked"] is True
    assert set(probe["thread_limits"].values()) == {"1"}


@pytest.mark.integration
def test_quantaxis_direct_smoke(tmp_path: Path) -> None:
    runner = _runner(
        env_name="VIBE_QE0_QUANTAXIS_PYTHON",
        engine="quantaxis",
        commit="a69e978a2e38d045a64c380cc3b5c9fa08fa4903",
        tmp_path=tmp_path,
    )
    _assert_isolation(runner, "quantaxis")

    result = runner.run(
        request_id="qe0-quantaxis-direct",
        operation="direct_smoke",
        timeout_seconds=30,
    ).response["result"]
    assert result["import"]["version"] == "2.1.0a2"
    assert result["import"]["boundary"] == "pinned_source_modules"
    assert result["import"]["top_level_package_executed"] is False
    assert result["import"]["source_sha256"] == {
        "calendar": "014ff7173c349da78bbf60dcad52227881d9f0a32d958d98973c4914e12acf45",
        "data_fq": "8ea6b152a4eff20bffba2216ae8dc88edbb3f0f11b0a220abb19c574cf459eff",
        "indicator_base": "fbbb3debfa0d061eb4d6e56acf783dea18a144f56ffc191769df075cf38c4737",
        "indicators": "94995068dbe73fdeddfea69bd51c0df52af6fc4567c5c432a265c2e3f9363ff5",
        "market_preset": "d789ed62fd173ce2102f87c22ec6e7c155f6c07180e383ff49965fbed70bf8fd",
        "position": "531b691e8a8791cf1a5e9136a980f5a6972e2e4f6fd78df994766213f80dda55",
        "qifi_account": "8b1cae0450d7c3cf9fb4f112191724096f09c84fd6d9c11662d2f13020166a9d",
    }
    assert result["adjust_prices"] == {
        "qfq_close": [9.70392157, 9.8, 9.8, 10.0],
        "hfq_close": [10.1, 10.2, 10.2, 10.40816327],
    }
    assert result["trading_calendar"] == [
        "2024-06-12",
        "2024-06-13",
        "2024-06-14",
        "2024-06-17",
    ]
    assert result["factor"]["ma2"] == [None, 10.15, 10.0, 9.9]
    assert result["account_backtest"] == {
        "order_status": "FINISHED",
        "volume_left": 0,
        "trade_count": 1,
        "position_count": 1,
    }

    replay = runner.run(
        request_id="qe0-quantaxis-direct-replay",
        operation="direct_smoke",
        timeout_seconds=30,
    ).response["result"]
    assert replay == result


@pytest.mark.integration
def test_vnpy_direct_smoke(tmp_path: Path) -> None:
    runner = _runner(
        env_name="VIBE_QE0_VNPY_PYTHON",
        engine="vnpy",
        commit="1b78494979deb4c4996f6b864f234d9839f2f239",
        tmp_path=tmp_path,
    )
    _assert_isolation(runner, "vnpy")

    result = runner.run(
        request_id="qe0-vnpy-direct",
        operation="direct_smoke",
    ).response["result"]
    assert result["import"] == {
        "version": "4.4.0",
        "source_sha256": {
            "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
            "trader_object": "bd360fc224ce22a3f7521bef67ea61125d43c410fa880b9667030ee254546a69",
            "trader_constant": "1361eb485eda9fd97bee3e68324d9b08a96fe927c37a99f8b74de981141e7a0f",
        },
    }
    assert result["event_count"] == 5
    assert result["order_statuses"] == ["NOTTRADED", "PARTTRADED", "ALLTRADED"]
    assert result["trade_volumes"] == [40, 60]
    assert result["net_position"] == 100

    replay = runner.run(
        request_id="qe0-vnpy-direct-replay",
        operation="direct_smoke",
    ).response["result"]
    assert replay == result
