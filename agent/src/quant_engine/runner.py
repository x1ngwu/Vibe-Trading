"""Fail-closed subprocess runner for the QE0 worker isolation PoC."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Mapping

from .protocol import (
    EngineIdentity,
    MAX_REQUEST_BYTES,
    ProtocolError,
    build_request,
    canonical_json,
    strict_json_loads,
    validate_request,
    validate_response,
)


_SECRET_MARKERS = (
    "API_KEY",
    "API_TOKEN",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET_KEY",
    "TUSHARE_TOKEN",
)


@dataclass(frozen=True)
class WorkerConfig:
    """Trusted local configuration for one exact worker environment."""

    engine: EngineIdentity
    python: Path
    script: Path
    snapshot_root: Path


@dataclass(frozen=True)
class RunResult:
    """Validated worker result and bounded diagnostics."""

    request: dict[str, Any]
    response: dict[str, Any]
    stderr: str
    elapsed_seconds: float
    peak_rss_kib: int | None


class WorkerExecutionError(RuntimeError):
    """Stable runner error that is safe to persist as PoC diagnostics."""

    def __init__(self, code: str, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr


class WorkerRunner:
    """Launch one request per process with strict JSONL and resource bounds."""

    def __init__(self, config: WorkerConfig, *, common_runtime: Path | None = None) -> None:
        self.config = config
        self.common_runtime = common_runtime
        self._validate_config()

    def _validate_config(self) -> None:
        python = self.config.python
        script = self.config.script
        root = self.config.snapshot_root
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("worker python must be an absolute executable file")
        if not script.is_absolute() or not script.is_file() or script.is_symlink():
            raise ValueError("worker script must be an absolute regular non-symlink file")
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValueError("snapshot_root must be an absolute real directory")
        if self.common_runtime is not None:
            if (
                not self.common_runtime.is_absolute()
                or not self.common_runtime.is_dir()
                or self.common_runtime.is_symlink()
            ):
                raise ValueError("common_runtime must be an absolute real directory")

    @staticmethod
    def _contains_secret_name(name: str) -> bool:
        upper = name.upper()
        return any(marker in upper for marker in _SECRET_MARKERS)

    def _worker_env(self, ephemeral_home: Path) -> dict[str, str]:
        """Construct a deterministic environment without inherited credentials."""

        env: dict[str, str] = {
            "HOME": str(ephemeral_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "Asia/Shanghai",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "VIBE_QUANT_NETWORK_DISABLED": "1",
            "VIBE_QUANT_SNAPSHOT_ROOT": str(self.config.snapshot_root.resolve(strict=True)),
        }
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            value = os.environ.get(key)
            if value and not self._contains_secret_name(key):
                env[key] = value
        if self.common_runtime is not None:
            env["PYTHONPATH"] = str(self.common_runtime.resolve(strict=True))
        return env

    @staticmethod
    def _validate_snapshot_path(path_text: str, root: Path) -> Path:
        """Reject traversal, symlinks and special files before worker launch."""

        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = root / candidate
        root_real = root.resolve(strict=True)

        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError as exc:
                raise WorkerExecutionError("PATH_VIOLATION", "snapshot path does not exist") from exc
            if stat.S_ISLNK(mode):
                raise WorkerExecutionError("PATH_VIOLATION", "snapshot path contains a symlink")

        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root_real)
        except ValueError as exc:
            raise WorkerExecutionError("PATH_VIOLATION", "snapshot path escapes snapshot root") from exc
        mode = resolved.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise WorkerExecutionError("PATH_VIOLATION", "snapshot path must be a regular file or directory")
        return resolved

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[bytes], *, grace_seconds: float = 0.5) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows fallback
                proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _rss_kib(pid: int) -> int | None:
        try:
            text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
        return None

    def run(
        self,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        snapshot_path: str | None = None,
        snapshot_sha256: str | None = None,
        timeout_seconds: float = 30.0,
        max_stdout_bytes: int = 1_048_576,
        max_stderr_bytes: int = 1_048_576,
        cancel_event: Event | None = None,
    ) -> RunResult:
        """Execute and validate one worker request."""

        normalized_snapshot: str | None = None
        if snapshot_path is not None:
            normalized_snapshot = str(self._validate_snapshot_path(snapshot_path, self.config.snapshot_root))
        request = build_request(
            request_id=request_id,
            engine=self.config.engine,
            operation=operation,
            payload=payload,
            snapshot_path=normalized_snapshot,
            snapshot_sha256=snapshot_sha256,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        validate_request(request, expected_engine=self.config.engine)
        request_line = (canonical_json(request) + "\n").encode("utf-8")
        if len(request_line) > MAX_REQUEST_BYTES:
            raise WorkerExecutionError(
                "REQUEST_LIMIT",
                f"request exceeds {MAX_REQUEST_BYTES} bytes",
            )

        home = Path(tempfile.mkdtemp(prefix="vibe-quant-worker-home-"))
        proc: subprocess.Popen[bytes] | None = None
        started = time.monotonic()
        stdout = bytearray()
        stderr = bytearray()
        peak_rss: int | None = None
        failure_code: str | None = None
        try:
            proc = subprocess.Popen(
                [str(self.config.python), "-B", str(self.config.script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.config.script.parent),
                env=self._worker_env(home),
                start_new_session=True,
            )
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            proc.stdin.write(request_line)
            proc.stdin.close()

            selector = selectors.DefaultSelector()
            for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)

            deadline = started + timeout_seconds
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                rss = self._rss_kib(proc.pid)
                if rss is not None:
                    peak_rss = max(peak_rss or 0, rss)
                if failure_code is None and cancel_event is not None and cancel_event.is_set():
                    failure_code = "WORKER_CANCELLED"
                    self._terminate_group(proc)
                elif failure_code is None and now >= deadline:
                    failure_code = "WORKER_TIMEOUT"
                    self._terminate_group(proc)

                for key, _ in selector.select(timeout=0.02):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = stdout if key.data == "stdout" else stderr
                    limit = max_stdout_bytes if key.data == "stdout" else max_stderr_bytes
                    remaining = max(0, limit - len(target))
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining and failure_code is None:
                        failure_code = "OUTPUT_LIMIT"
                        self._terminate_group(proc)

                if failure_code is not None and proc.poll() is not None and not selector.get_map():
                    break

            exit_code = proc.wait(timeout=1)
            elapsed = time.monotonic() - started
            stderr_text = stderr.decode("utf-8", errors="replace")
            if failure_code is not None:
                message = {
                    "WORKER_TIMEOUT": "worker exceeded timeout",
                    "WORKER_CANCELLED": "worker was cancelled",
                    "OUTPUT_LIMIT": "worker exceeded stdout/stderr limit",
                }[failure_code]
                raise WorkerExecutionError(failure_code, message, stderr=stderr_text)
            if exit_code != 0:
                raise WorkerExecutionError(
                    "WORKER_CRASH",
                    f"worker exited with code {exit_code}",
                    stderr=stderr_text,
                )

            try:
                stdout_text = stdout.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WorkerExecutionError("INVALID_RESPONSE", str(exc), stderr=stderr_text) from exc
            lines = stdout_text.splitlines()
            if len(lines) != 1 or not stdout_text.endswith("\n"):
                raise WorkerExecutionError(
                    "PROTOCOL_POLLUTION",
                    "stdout must contain exactly one newline-terminated JSON object",
                    stderr=stderr_text,
                )
            try:
                decoded = strict_json_loads(lines[0])
                response = validate_response(decoded, request=request, expected_engine=self.config.engine)
            except ProtocolError as exc:
                code = "INVALID_RESPONSE" if exc.code == "INVALID_JSON" else exc.code
                raise WorkerExecutionError(code, str(exc), stderr=stderr_text) from exc
            if response["status"] == "error":
                error = response["error"]
                raise WorkerExecutionError(error["code"], error["message"], stderr=stderr_text)
            return RunResult(
                request=request,
                response=response,
                stderr=stderr_text,
                elapsed_seconds=elapsed,
                peak_rss_kib=peak_rss,
            )
        finally:
            if proc is not None and proc.poll() is None:
                self._terminate_group(proc)
            shutil.rmtree(home, ignore_errors=True)
