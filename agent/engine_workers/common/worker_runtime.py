"""Stdlib-only runtime shared by isolated QE0 workers.

This file is loaded from the checked-out source tree, not from Vibe's Python
environment.  Engine imports happen only after the protocol, path and network
guards have been installed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import traceback
from typing import Any, Callable, Mapping


PROTOCOL_NAME = "vibe.quant-engine.jsonl"
SCHEMA_VERSION = "1.0"
MAX_REQUEST_BYTES = 1_048_576
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_KEYS = {
    "protocol",
    "schema_version",
    "request_id",
    "engine",
    "operation",
    "payload",
    "snapshot",
    "limits",
    "content_sha256",
}
_ENGINE_KEYS = {"name", "commit"}
_SNAPSHOT_KEYS = {"path", "sha256"}
_LIMIT_KEYS = {"timeout_seconds", "max_stdout_bytes", "max_stderr_bytes"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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


class WorkerError(RuntimeError):
    """An operation or protocol error safe to return over the worker boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _strict_json_loads(value: str | bytes | bytearray) -> Any:
    def reject_constant(constant: str) -> None:
        raise WorkerError(
            "INVALID_JSON_VALUE",
            f"non-finite JSON constant is not allowed: {constant}",
        )

    try:
        return json.loads(value, parse_constant=reject_constant)
    except WorkerError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkerError("INVALID_JSON", str(exc)) from exc


def _content_sha256(value: Mapping[str, Any]) -> str:
    unhashed = dict(value)
    unhashed.pop("content_sha256", None)
    return hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()


def _install_network_guard() -> None:
    if os.environ.get("VIBE_QUANT_NETWORK_DISABLED") != "1":
        raise WorkerError("NETWORK_GUARD_REQUIRED", "worker must be launched with networking disabled")

    original_socket = socket.socket

    class NetworkDeniedSocket(original_socket):
        def bind(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("quant worker network disabled")

        def connect(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("quant worker network disabled")

        def connect_ex(self, *args: Any, **kwargs: Any) -> int:
            raise PermissionError("quant worker network disabled")

        def listen(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("quant worker network disabled")

        def send(self, *args: Any, **kwargs: Any) -> int:
            raise PermissionError("quant worker network disabled")

        def sendall(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("quant worker network disabled")

        def sendto(self, *args: Any, **kwargs: Any) -> int:
            raise PermissionError("quant worker network disabled")

        def sendmsg(self, *args: Any, **kwargs: Any) -> int:
            raise PermissionError("quant worker network disabled")

    def deny_connection(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("quant worker network disabled")

    socket.socket = NetworkDeniedSocket
    socket.create_connection = deny_connection
    socket.getaddrinfo = deny_connection
    socket.gethostbyaddr = deny_connection
    socket.gethostbyname = deny_connection
    socket.gethostbyname_ex = deny_connection
    socket.getnameinfo = deny_connection


SNAPSHOT_MANIFEST_VERSION = "vibe.snapshot-manifest.v1"


def _hash_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise WorkerError("PATH_VIOLATION", "snapshot file cannot be read") from exc
    return digest.hexdigest()


def _snapshot_sha256(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise WorkerError("PATH_VIOLATION", "snapshot path cannot be inspected") from exc
    if stat.S_ISLNK(mode):
        raise WorkerError("PATH_VIOLATION", "snapshot path contains a symlink")
    if stat.S_ISREG(mode):
        return _hash_regular_file(path)
    if not stat.S_ISDIR(mode):
        raise WorkerError("PATH_VIOLATION", "snapshot path must be a regular file or directory")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(path.rglob("*"), key=lambda child: child.relative_to(path).as_posix())
        for child in children:
            child_mode = child.lstat().st_mode
            relative = child.relative_to(path).as_posix()
            if stat.S_ISLNK(child_mode):
                raise WorkerError("PATH_VIOLATION", f"snapshot manifest contains symlink: {relative}")
            if stat.S_ISDIR(child_mode):
                continue
            if not stat.S_ISREG(child_mode):
                raise WorkerError("PATH_VIOLATION", f"snapshot manifest contains special file: {relative}")
            entries.append(
                {
                    "path": relative,
                    "size": child.stat().st_size,
                    "sha256": _hash_regular_file(child),
                }
            )
    except WorkerError:
        raise
    except OSError as exc:
        raise WorkerError("PATH_VIOLATION", "snapshot directory cannot be read") from exc

    manifest = {"version": SNAPSHOT_MANIFEST_VERSION, "files": entries}
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _validate_snapshot(request: Mapping[str, Any]) -> None:
    snapshot = request.get("snapshot")
    if snapshot is None:
        return
    if not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_KEYS:
        raise WorkerError("INVALID_SCHEMA", "invalid snapshot object")
    root_text = os.environ.get("VIBE_QUANT_SNAPSHOT_ROOT")
    path_text = snapshot.get("path")
    if not root_text or not isinstance(path_text, str):
        raise WorkerError("PATH_VIOLATION", "snapshot root/path is unavailable")
    snapshot_sha256 = snapshot.get("sha256")
    if not isinstance(snapshot_sha256, str) or not _SHA256_RE.fullmatch(snapshot_sha256):
        raise WorkerError("INVALID_SCHEMA", "snapshot.sha256 must be lowercase SHA-256")
    root = Path(root_text).resolve(strict=True)
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = root / candidate
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise WorkerError("PATH_VIOLATION", "snapshot path does not exist") from exc
        if stat.S_ISLNK(mode):
            raise WorkerError("PATH_VIOLATION", "snapshot path contains a symlink")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkerError("PATH_VIOLATION", "snapshot path escapes snapshot root") from exc
    mode = resolved.stat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise WorkerError("PATH_VIOLATION", "snapshot path must be a regular file or directory")
    actual_snapshot_sha256 = _snapshot_sha256(resolved)
    if actual_snapshot_sha256 != snapshot_sha256:
        raise WorkerError("SNAPSHOT_HASH_MISMATCH", "snapshot content does not match snapshot.sha256")


def _validate_limits(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _LIMIT_KEYS:
        raise WorkerError("INVALID_SCHEMA", "invalid limits object")
    timeout = value.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise WorkerError("INVALID_LIMIT", "timeout_seconds must be finite")
    if timeout <= 0 or timeout > 3600:
        raise WorkerError("INVALID_LIMIT", "timeout_seconds must be in (0, 3600]")
    for key in ("max_stdout_bytes", "max_stderr_bytes"):
        limit = value.get(key)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 256 or limit > 16_777_216:
            raise WorkerError("INVALID_LIMIT", f"{key} must be an integer in [256, 16777216]")


def _validate_request(request: Any, *, engine_name: str, engine_commit: str) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != _REQUEST_KEYS:
        raise WorkerError("INVALID_SCHEMA", "request keys do not match QE0 schema")
    if request.get("protocol") != PROTOCOL_NAME or request.get("schema_version") != SCHEMA_VERSION:
        raise WorkerError("PROTOCOL_MISMATCH", "unsupported protocol or schema version")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkerError("INVALID_SCHEMA", "invalid request_id")
    engine = request.get("engine")
    if not isinstance(engine, Mapping) or set(engine) != _ENGINE_KEYS:
        raise WorkerError("INVALID_SCHEMA", "invalid engine identity")
    if engine.get("name") != engine_name or engine.get("commit") != engine_commit:
        raise WorkerError("ENGINE_MISMATCH", "request engine does not match this worker")
    operation = request.get("operation")
    if not isinstance(operation, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", operation):
        raise WorkerError("INVALID_SCHEMA", "invalid operation")
    if not isinstance(request.get("payload"), Mapping):
        raise WorkerError("INVALID_SCHEMA", "payload must be an object")
    supplied_hash = request.get("content_sha256")
    if not isinstance(supplied_hash, str) or not _SHA256_RE.fullmatch(supplied_hash):
        raise WorkerError("INVALID_SCHEMA", "content_sha256 must be lowercase SHA-256")
    if supplied_hash != _content_sha256(request):
        raise WorkerError("HASH_MISMATCH", "request content hash mismatch")
    _validate_limits(request.get("limits"))
    _validate_snapshot(request)
    return dict(request)


def security_probe() -> dict[str, Any]:
    suspicious = sorted(
        key
        for key in os.environ
        if any(marker in key.upper() for marker in _SECRET_MARKERS)
    )
    network_blocked = False
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    except PermissionError:
        network_blocked = True
    except OSError:
        network_blocked = False
    name_resolution_blocked = False
    try:
        socket.getaddrinfo("example.invalid", 443)
    except PermissionError:
        name_resolution_blocked = True
    except OSError:
        name_resolution_blocked = False
    bind_blocked = False
    bind_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bind_probe.bind(("127.0.0.1", 0))
    except PermissionError:
        bind_blocked = True
    finally:
        bind_probe.close()
    datagram_blocked = False
    datagram_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        datagram_probe.sendto(b"qe0", ("127.0.0.1", 9))
    except PermissionError:
        datagram_blocked = True
    finally:
        datagram_probe.close()
    return {
        "network_blocked": network_blocked,
        "name_resolution_blocked": name_resolution_blocked,
        "bind_blocked": bind_blocked,
        "datagram_blocked": datagram_blocked,
        "secret_named_environment": suspicious,
        "timezone": os.environ.get("TZ"),
        "locale": os.environ.get("LC_ALL"),
        "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        "thread_limits": {
            key: os.environ.get(key)
            for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
        },
    }


def _security_probe_handler(
    payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    del payload, snapshot
    return security_probe()


def _response(
    *,
    engine_name: str,
    engine_commit: str,
    request_id: str,
    request_sha256: str,
    result: Mapping[str, Any] | None = None,
    error: WorkerError | None = None,
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "engine": {"name": engine_name, "commit": engine_commit},
        "request_sha256": request_sha256,
        "status": "error" if error else "ok",
        "result": None if error else dict(result or {}),
        "error": {"code": error.code, "message": str(error)} if error else None,
    }


def run_worker(
    *,
    engine_name: str,
    engine_commit: str,
    handlers: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]]],
) -> int:
    """Read one request, execute one handler, and emit exactly one JSON line."""

    if not _COMMIT_RE.fullmatch(engine_commit):
        raise RuntimeError("worker source contains an invalid engine commit")
    wire_stdout = sys.stdout
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    trailing = sys.stdin.buffer.read(1)
    request: dict[str, Any] = {}
    try:
        _install_network_guard()
        if len(raw) > MAX_REQUEST_BYTES or trailing or not raw.endswith(b"\n"):
            raise WorkerError("INVALID_SCHEMA", "stdin must contain exactly one bounded JSON line")
        request = _strict_json_loads(raw)
        request = _validate_request(request, engine_name=engine_name, engine_commit=engine_commit)
        operation = request["operation"]
        if operation == "security_probe":
            handler = _security_probe_handler
        else:
            handler = handlers.get(operation)
            if handler is None:
                raise WorkerError("UNSUPPORTED_OPERATION", f"unsupported operation: {operation}")
        with contextlib.redirect_stdout(sys.stderr):
            result = handler(request["payload"], request["snapshot"])
        if not isinstance(result, Mapping):
            raise WorkerError("INVALID_ENGINE_OUTPUT", "worker handler returned a non-object")
        try:
            canonical_json(result)
        except (TypeError, ValueError) as exc:
            raise WorkerError("INVALID_ENGINE_OUTPUT", "worker handler returned invalid JSON values") from exc
        response = _response(
            engine_name=engine_name,
            engine_commit=engine_commit,
            request_id=request["request_id"],
            request_sha256=request["content_sha256"],
            result=result,
        )
    except WorkerError as exc:
        response = _response(
            engine_name=engine_name,
            engine_commit=engine_commit,
            request_id=str(request.get("request_id", "invalid")),
            request_sha256=str(request.get("content_sha256", "0" * 64)),
            error=exc,
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        response = _response(
            engine_name=engine_name,
            engine_commit=engine_commit,
            request_id=str(request.get("request_id", "invalid")),
            request_sha256=str(request.get("content_sha256", "0" * 64)),
            error=WorkerError("ENGINE_FAILURE", f"{type(exc).__name__}: {exc}"),
        )
    wire_stdout.write(canonical_json(response) + "\n")
    wire_stdout.flush()
    return 0
