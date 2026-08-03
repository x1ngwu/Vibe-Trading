"""Opt-in QE0 direct integration tests for the two isolated environments."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
from types import ModuleType

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
        "parameters": "dfb09865c6d6016cfea5c2a6009b09353fbe6aa5416a2df53ede82d2ca8ab773",
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
            "package_init": "ba16287a3acd984a6e68c3e373441c7e8af623ad9df74837227368a4456915a8",
            "event_init": "81752eb9db5a9e9bdf7024f9a8821cf569c5994eceff9194a6ecd725dc8ed365",
            "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
            "trader_init": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "trader_locale_init": "3138d59a9d7bf99cdcc683778e402f0dd7fabcf9698c3ec2820e44508ac0661c",
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


PROVENANCE_FILES = {
    "quantaxis": (
        "data_fq",
        "indicator_base",
        "indicators",
        "calendar",
        "market_preset",
        "position",
        "qifi_account",
        "parameters",
    ),
    "vnpy": (
        "package_init",
        "event_init",
        "event_engine",
        "trader_init",
        "trader_locale_init",
        "trader_object",
        "trader_constant",
    ),
}


class _FakeDistribution:
    def __init__(self, *, version: str, site_packages: Path) -> None:
        self.version = version
        self.site_packages = site_packages

    def locate_file(self, path: str) -> Path:
        return self.site_packages / path


def _load_worker_module(engine: str) -> ModuleType:
    path = AGENT_ROOT / "engine_workers" / engine / "worker.py"
    spec = importlib.util.spec_from_file_location(f"_qe0_{engine}_worker_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_fake_distribution(
    *,
    engine: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, Path]:
    worker = _load_worker_module(engine)
    package_name = "QUANTAXIS" if engine == "quantaxis" else "vnpy"
    root = tmp_path / "site-packages" / package_name
    root.mkdir(parents=True)
    if engine == "quantaxis":
        (root / "__init__.py").write_text("# package marker\n", encoding="utf-8")

    expected: dict[str, str] = {}
    for name, relative in worker.SOURCE_RELATIVE_PATHS.items():
        path = root.joinpath(*relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"{engine}:{name}\n".encode()
        path.write_bytes(content)
        expected[name] = hashlib.sha256(content).hexdigest()

    assert set(worker.SOURCE_RELATIVE_PATHS) == set(PROVENANCE_FILES[engine])
    monkeypatch.setattr(worker, "EXPECTED_SOURCE_SHA256", expected)
    monkeypatch.setattr(
        worker,
        "distribution",
        lambda name: _FakeDistribution(
            version=worker.EXPECTED_ENGINE_VERSION,
            site_packages=tmp_path / "site-packages",
        ),
    )
    return worker, root


@pytest.mark.parametrize("engine", ["quantaxis", "vnpy"])
def test_capabilities_fail_closed_when_engine_is_missing(
    engine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _load_worker_module(engine)

    def missing_distribution(name: str) -> None:
        raise worker.PackageNotFoundError(name)

    monkeypatch.setattr(worker, "distribution", missing_distribution)
    with pytest.raises(worker.WorkerError) as raised:
        worker.capabilities({}, None)

    assert raised.value.code == "ENGINE_UNAVAILABLE"


@pytest.mark.parametrize("engine", ["quantaxis", "vnpy"])
def test_capabilities_fail_closed_on_wrong_engine_version(
    engine: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _load_worker_module(engine)
    monkeypatch.setattr(
        worker,
        "distribution",
        lambda name: _FakeDistribution(version="0.0.invalid", site_packages=tmp_path),
    )

    with pytest.raises(worker.WorkerError) as raised:
        worker.capabilities({}, None)

    assert raised.value.code == "ENGINE_VERSION_MISMATCH"


@pytest.mark.parametrize(
    ("engine", "tampered_name"),
    [
        (engine, name)
        for engine, names in PROVENANCE_FILES.items()
        for name in names
    ],
)
def test_capabilities_fail_closed_when_any_executed_source_is_tampered(
    engine: str,
    tampered_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, root = _prepare_fake_distribution(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    relative = worker.SOURCE_RELATIVE_PATHS[tampered_name]
    root.joinpath(*relative).write_bytes(b"tampered\n")

    with pytest.raises(worker.WorkerError) as raised:
        worker.capabilities({}, None)

    assert raised.value.code == "ENGINE_PROVENANCE_MISMATCH"


def test_capabilities_advertise_operations_only_after_provenance_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for engine in ("quantaxis", "vnpy"):
        worker, _ = _prepare_fake_distribution(
            engine=engine,
            tmp_path=tmp_path / engine,
            monkeypatch=monkeypatch,
        )
        result = worker.capabilities({}, None)
        assert result["engine_version"] == worker.EXPECTED_ENGINE_VERSION
        if engine == "vnpy":
            assert {
                name: result["source_sha256"][name]
                for name in worker.EXPECTED_SOURCE_SHA256
            } == worker.EXPECTED_SOURCE_SHA256
            assert set(result["source_sha256"]) == (
                set(worker.EXPECTED_SOURCE_SHA256) | set(worker.LOCAL_SOURCE_PATHS)
            )
        else:
            assert result["source_sha256"] == worker.EXPECTED_SOURCE_SHA256
        assert result["operations"]["direct_smoke"] == "poc"
        if engine == "quantaxis":
            assert result["operations"]["adjust_prices"] == "qe2"
            assert result["operations"]["trading_calendar"] == "qe2"
            assert result["operations"]["compute_factors"] == {
                "status": "qe2",
                "whitelist": ["ma", "ema"],
            }
        else:
            assert result["operations"]["event_replay"] == "qe6_1"
            assert result["operations"]["ordinary_replay"] == "qe6_2"
            assert result["operations"]["china_a_replay"] == "qe6_3"
            assert result["operations"]["normalize_ledger"] == (
                "qe6_2_ordinary_only"
            )
            assert result["operations"]["backtest"] == "not_available"
