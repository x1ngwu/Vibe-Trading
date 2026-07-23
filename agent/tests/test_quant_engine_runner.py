"""QE0 subprocess, path, output and environment isolation tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import sys
from threading import Event, Timer
import time

import pytest

from src.quant_engine import (
    EngineIdentity,
    WorkerConfig,
    WorkerExecutionError,
    WorkerRunner,
    compute_snapshot_sha256,
)
from src.quant_engine.protocol import build_request, canonical_json, content_sha256


ENGINE = EngineIdentity("fake", "a" * 40)
AGENT_ROOT = Path(__file__).resolve().parents[1]
FAKE_WORKER = (AGENT_ROOT / "tests" / "fixtures" / "fake_quant_worker.py").resolve()
COMMON_RUNTIME = (AGENT_ROOT / "engine_workers" / "common").resolve()


@pytest.fixture
def snapshot_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "snapshot.json").write_text("{}\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def runner(snapshot_root: Path) -> WorkerRunner:
    return WorkerRunner(
        WorkerConfig(
            engine=ENGINE,
            python=Path(sys.executable).resolve(),
            script=FAKE_WORKER,
            snapshot_root=snapshot_root,
        ),
        common_runtime=COMMON_RUNTIME,
    )


def _error(runner: WorkerRunner, operation: str, **kwargs: object) -> WorkerExecutionError:
    with pytest.raises(WorkerExecutionError) as raised:
        runner.run(request_id=f"qe0-{operation}", operation=operation, **kwargs)
    return raised.value


def test_jsonl_stdout_is_clean_and_diagnostics_use_stderr(runner: WorkerRunner) -> None:
    result = runner.run(request_id="qe0-echo", operation="echo", payload={"answer": 42})

    assert result.response["result"] == {"payload": {"answer": 42}, "snapshot": None}
    assert result.stderr.strip() == "fake worker diagnostic"
    assert result.peak_rss_kib is None or result.peak_rss_kib > 0


def test_protocol_pollution_crash_and_output_limit_are_stable(runner: WorkerRunner) -> None:
    assert _error(runner, "pollute_stdout").code == "PROTOCOL_POLLUTION"
    assert _error(runner, "truncate_stdout").code == "PROTOCOL_POLLUTION"
    assert _error(runner, "crash").code == "WORKER_CRASH"
    assert _error(runner, "large_stderr", max_stderr_bytes=256).code == "OUTPUT_LIMIT"


def test_oversized_request_is_rejected_before_worker_launch(runner: WorkerRunner) -> None:
    error = _error(runner, "echo", payload={"oversized": "x" * 1_048_576})
    assert error.code == "REQUEST_LIMIT"


def test_worker_revalidates_limits_before_dispatch(runner: WorkerRunner) -> None:
    request = build_request(
        request_id="qe0-invalid-worker-limit",
        engine=ENGINE,
        operation="echo",
    )
    request["limits"]["timeout_seconds"] = 0
    request["content_sha256"] = content_sha256(request)

    completed = subprocess.run(
        [str(runner.config.python), "-B", str(runner.config.script)],
        input=(canonical_json(request) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(runner.config.script.parent),
        env=runner._worker_env(runner.config.snapshot_root),
        check=False,
    )
    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["status"] == "error"
    assert response["error"]["code"] == "INVALID_LIMIT"


def test_timeout_and_cancel_fail_closed(runner: WorkerRunner) -> None:
    timed_out = _error(runner, "sleep_forever", timeout_seconds=0.1)
    assert timed_out.code == "WORKER_TIMEOUT"

    cancelled = Event()
    timer = Timer(0.1, cancelled.set)
    timer.start()
    try:
        error = _error(runner, "sleep_forever", timeout_seconds=5, cancel_event=cancelled)
    finally:
        timer.cancel()
    assert error.code == "WORKER_CANCELLED"


def test_timeout_terminates_worker_process_group(runner: WorkerRunner, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    error = _error(
        runner,
        "spawn_and_sleep",
        payload={"pid_file": str(pid_file)},
        timeout_seconds=0.3,
    )
    assert error.code == "WORKER_TIMEOUT"
    child_pid = int(pid_file.read_text(encoding="utf-8"))

    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        stat_text = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8")
        if stat_text.split()[2] == "Z":
            break
        time.sleep(0.02)
    if Path(f"/proc/{child_pid}").exists():
        assert Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2] == "Z"


def test_snapshot_path_is_bounded_and_rejects_symlinks_and_special_files(
    runner: WorkerRunner,
    snapshot_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    link = snapshot_root / "link.json"
    link.symlink_to(outside)
    fifo = snapshot_root / "pipe"
    os.mkfifo(fifo)

    for candidate in ("../outside.json", str(outside), str(link), str(fifo)):
        error = _error(runner, "echo", snapshot_path=candidate)
        assert error.code == "PATH_VIOLATION"


def test_snapshot_path_and_hash_reach_worker_unchanged(runner: WorkerRunner) -> None:
    snapshot_file = runner.config.snapshot_root / "snapshot.json"
    snapshot_hash = compute_snapshot_sha256(snapshot_file)
    result = runner.run(
        request_id="qe0-snapshot",
        operation="echo",
        snapshot_path="snapshot.json",
        snapshot_sha256=snapshot_hash,
    )

    assert result.response["result"]["snapshot"] == {
        "path": str(snapshot_file.resolve()),
        "sha256": snapshot_hash,
    }


def test_snapshot_hash_is_required_and_checked_before_launch(runner: WorkerRunner) -> None:
    missing = _error(runner, "echo", snapshot_path="snapshot.json")
    assert missing.code == "SNAPSHOT_HASH_REQUIRED"

    mismatch = _error(
        runner,
        "echo",
        snapshot_path="snapshot.json",
        snapshot_sha256="0" * 64,
    )
    assert mismatch.code == "SNAPSHOT_HASH_MISMATCH"


def test_directory_snapshot_manifest_is_deterministic_and_content_bound(
    runner: WorkerRunner,
) -> None:
    first = runner.config.snapshot_root / "first"
    second = runner.config.snapshot_root / "second"
    for directory in (first, second):
        (directory / "nested").mkdir(parents=True)
    (first / "b.json").write_text("{\"b\":2}\n", encoding="utf-8")
    (first / "nested" / "a.csv").write_text("a\n1\n", encoding="utf-8")
    (second / "nested" / "a.csv").write_text("a\n1\n", encoding="utf-8")
    (second / "b.json").write_text("{\"b\":2}\n", encoding="utf-8")

    first_hash = compute_snapshot_sha256(first)
    assert compute_snapshot_sha256(second) == first_hash
    result = runner.run(
        request_id="qe0-directory-snapshot",
        operation="echo",
        snapshot_path=str(first),
        snapshot_sha256=first_hash,
    )
    assert result.response["result"]["snapshot"]["sha256"] == first_hash

    (first / "nested" / "a.csv").write_text("a\n2\n", encoding="utf-8")
    assert compute_snapshot_sha256(first) != first_hash
    mismatch = _error(
        runner,
        "echo",
        snapshot_path=str(first),
        snapshot_sha256=first_hash,
    )
    assert mismatch.code == "SNAPSHOT_HASH_MISMATCH"


def test_directory_snapshot_manifest_rejects_nested_symlink(
    runner: WorkerRunner,
) -> None:
    directory = runner.config.snapshot_root / "with-link"
    directory.mkdir()
    (directory / "link.json").symlink_to(runner.config.snapshot_root / "snapshot.json")

    with pytest.raises(WorkerExecutionError) as raised:
        compute_snapshot_sha256(directory)

    assert raised.value.code == "PATH_VIOLATION"


def test_worker_revalidates_snapshot_after_runner_side_hashing(runner: WorkerRunner) -> None:
    snapshot_file = runner.config.snapshot_root / "snapshot.json"
    snapshot_hash = compute_snapshot_sha256(snapshot_file)
    request = build_request(
        request_id="qe0-worker-snapshot-tamper",
        engine=ENGINE,
        operation="echo",
        snapshot_path=str(snapshot_file),
        snapshot_sha256=snapshot_hash,
    )
    snapshot_file.write_text("{\"tampered\":true}\n", encoding="utf-8")

    completed = subprocess.run(
        [str(runner.config.python), "-B", str(runner.config.script)],
        input=(canonical_json(request) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(runner.config.script.parent),
        env=runner._worker_env(runner.config.snapshot_root),
        check=False,
    )
    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["status"] == "error"
    assert response["error"]["code"] == "SNAPSHOT_HASH_MISMATCH"


def test_worker_gets_no_inherited_secrets_and_deterministic_environment(
    runner: WorkerRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("DATABASE_PASSWORD", "must-not-cross-boundary")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/must-not-cross-boundary")

    result = runner.run(request_id="qe0-security", operation="security_probe")
    probe = result.response["result"]

    assert probe["secret_named_environment"] == []
    assert probe["network_blocked"] is True
    assert probe["name_resolution_blocked"] is True
    assert probe["bind_blocked"] is True
    assert probe["datagram_blocked"] is True
    assert probe["timezone"] == "Asia/Shanghai"
    assert probe["locale"] == "C.UTF-8"
    assert probe["python_hash_seed"] == "0"
    assert set(probe["thread_limits"].values()) == {"1"}
    assert "LD_LIBRARY_PATH" not in runner._worker_env(runner.config.snapshot_root)


def test_invalid_engine_output_fails_with_stable_error(runner: WorkerRunner) -> None:
    assert _error(runner, "invalid_output").code == "INVALID_ENGINE_OUTPUT"
    assert _error(runner, "non_finite_output").code == "INVALID_ENGINE_OUTPUT"


def test_unsupported_operation_is_a_worker_error(runner: WorkerRunner) -> None:
    assert _error(runner, "backtest").code == "UNSUPPORTED_OPERATION"
