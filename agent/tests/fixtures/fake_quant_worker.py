"""Deterministic stdio worker used by the QE0 protocol isolation tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

from worker_runtime import run_worker


ENGINE_NAME = "fake"
ENGINE_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def echo(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    print("fake worker diagnostic")
    return {"payload": dict(payload), "snapshot": dict(snapshot) if snapshot else None}


def pollute_stdout(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    os.write(1, b"not-json\n")
    return {}


def crash(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    os._exit(23)


def truncate_stdout(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    os.write(1, b'{"partial":')
    os._exit(0)


def sleep_forever(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    time.sleep(60)
    return {}


def spawn_and_sleep(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(str(payload["pid_file"])).write_text(str(child.pid), encoding="utf-8")
    time.sleep(60)
    return {}


def large_stderr(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    os.write(sys.stderr.fileno(), b"x" * 4096)
    return {}


def invalid_output(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return {"not_json": object()}


def non_finite_output(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return {"not_finite": float("nan")}


if __name__ == "__main__":
    raise SystemExit(
        run_worker(
            engine_name=ENGINE_NAME,
            engine_commit=ENGINE_COMMIT,
            handlers={
                "crash": crash,
                "echo": echo,
                "invalid_output": invalid_output,
                "large_stderr": large_stderr,
                "non_finite_output": non_finite_output,
                "pollute_stdout": pollute_stdout,
                "sleep_forever": sleep_forever,
                "spawn_and_sleep": spawn_and_sleep,
                "truncate_stdout": truncate_stdout,
            },
        )
    )
