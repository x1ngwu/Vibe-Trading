"""Strict, versioned JSONL contract for isolated quant-engine workers.

QE0 intentionally keeps this contract small.  The complete EngineRequest and
BacktestRun models are a QE1 deliverable; this module proves that version and
engine negotiation fail closed before any external-engine operation starts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_NAME = "vibe.quant-engine.jsonl"
SCHEMA_VERSION = "1.0"
MAX_REQUEST_BYTES = 1_048_576

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
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
_RESPONSE_KEYS = {
    "protocol",
    "schema_version",
    "request_id",
    "engine",
    "request_sha256",
    "status",
    "result",
    "error",
}


class ProtocolError(ValueError):
    """A stable fail-closed protocol validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EngineIdentity:
    """An exact external engine identity."""

    name: str
    commit: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "commit": self.commit}


def canonical_json(value: Any) -> str:
    """Return deterministic JSON and reject NaN/Infinity."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError("INVALID_JSON_VALUE", str(exc)) from exc


def content_sha256(value: Mapping[str, Any]) -> str:
    """Hash a request-like mapping without its self-referential hash field."""

    unhashed = dict(value)
    unhashed.pop("content_sha256", None)
    return hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()


def build_request(
    *,
    request_id: str,
    engine: EngineIdentity,
    operation: str,
    payload: Mapping[str, Any] | None = None,
    snapshot_path: str | None = None,
    snapshot_sha256: str | None = None,
    timeout_seconds: float = 30.0,
    max_stdout_bytes: int = 1_048_576,
    max_stderr_bytes: int = 1_048_576,
) -> dict[str, Any]:
    """Build and validate one canonical QE0 request."""

    request: dict[str, Any] = {
        "protocol": PROTOCOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "engine": engine.as_dict(),
        "operation": operation,
        "payload": dict(payload or {}),
        "snapshot": (
            {"path": snapshot_path, "sha256": snapshot_sha256}
            if snapshot_path is not None
            else None
        ),
        "limits": {
            "timeout_seconds": timeout_seconds,
            "max_stdout_bytes": max_stdout_bytes,
            "max_stderr_bytes": max_stderr_bytes,
        },
    }
    request["content_sha256"] = content_sha256(request)
    validate_request(request, expected_engine=engine)
    return request


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ProtocolError(
            "INVALID_SCHEMA",
            f"{where} keys mismatch; unknown={unknown}, missing={missing}",
        )


def _validate_engine(value: Any, *, expected: EngineIdentity | None = None) -> EngineIdentity:
    if not isinstance(value, Mapping):
        raise ProtocolError("INVALID_SCHEMA", "engine must be an object")
    _require_exact_keys(value, _ENGINE_KEYS, "engine")
    name = value.get("name")
    commit = value.get("commit")
    if not isinstance(name, str) or not name or not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ProtocolError("INVALID_SCHEMA", "engine name and 40-character lowercase commit are required")
    identity = EngineIdentity(name=name, commit=commit)
    if expected is not None and identity != expected:
        raise ProtocolError(
            "ENGINE_MISMATCH",
            f"expected {expected.name}@{expected.commit}, got {identity.name}@{identity.commit}",
        )
    return identity


def _validate_limits(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ProtocolError("INVALID_SCHEMA", "limits must be an object")
    _require_exact_keys(value, _LIMIT_KEYS, "limits")
    timeout = value.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise ProtocolError("INVALID_LIMIT", "timeout_seconds must be finite")
    if timeout <= 0 or timeout > 3600:
        raise ProtocolError("INVALID_LIMIT", "timeout_seconds must be in (0, 3600]")
    for key in ("max_stdout_bytes", "max_stderr_bytes"):
        limit = value.get(key)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 256 or limit > 16_777_216:
            raise ProtocolError("INVALID_LIMIT", f"{key} must be an integer in [256, 16777216]")


def validate_request(value: Any, *, expected_engine: EngineIdentity | None = None) -> dict[str, Any]:
    """Strictly validate one decoded request and its content hash."""

    if not isinstance(value, Mapping):
        raise ProtocolError("INVALID_SCHEMA", "request must be an object")
    _require_exact_keys(value, _REQUEST_KEYS, "request")
    if value.get("protocol") != PROTOCOL_NAME or value.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("PROTOCOL_MISMATCH", "unsupported protocol or schema version")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ProtocolError("INVALID_SCHEMA", "invalid request_id")
    _validate_engine(value.get("engine"), expected=expected_engine)
    operation = value.get("operation")
    if not isinstance(operation, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", operation):
        raise ProtocolError("INVALID_SCHEMA", "invalid operation")
    if not isinstance(value.get("payload"), Mapping):
        raise ProtocolError("INVALID_SCHEMA", "payload must be an object")
    snapshot = value.get("snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, Mapping):
            raise ProtocolError("INVALID_SCHEMA", "snapshot must be null or an object")
        _require_exact_keys(snapshot, _SNAPSHOT_KEYS, "snapshot")
        if not isinstance(snapshot.get("path"), str) or not snapshot.get("path"):
            raise ProtocolError("INVALID_SCHEMA", "snapshot.path is required")
        snapshot_hash = snapshot.get("sha256")
        if snapshot_hash is not None and (
            not isinstance(snapshot_hash, str) or not _SHA256_RE.fullmatch(snapshot_hash)
        ):
            raise ProtocolError("INVALID_SCHEMA", "snapshot.sha256 must be null or lowercase SHA-256")
    _validate_limits(value.get("limits"))
    supplied_hash = value.get("content_sha256")
    if not isinstance(supplied_hash, str) or not _SHA256_RE.fullmatch(supplied_hash):
        raise ProtocolError("INVALID_SCHEMA", "content_sha256 must be lowercase SHA-256")
    if supplied_hash != content_sha256(value):
        raise ProtocolError("HASH_MISMATCH", "request content hash mismatch")
    return dict(value)


def validate_response(
    value: Any,
    *,
    request: Mapping[str, Any],
    expected_engine: EngineIdentity,
) -> dict[str, Any]:
    """Validate that a response belongs to the exact negotiated request."""

    if not isinstance(value, Mapping):
        raise ProtocolError("INVALID_RESPONSE", "response must be an object")
    _require_exact_keys(value, _RESPONSE_KEYS, "response")
    if value.get("protocol") != PROTOCOL_NAME or value.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("PROTOCOL_MISMATCH", "worker returned unsupported protocol or schema")
    _validate_engine(value.get("engine"), expected=expected_engine)
    if value.get("request_id") != request.get("request_id"):
        raise ProtocolError("REQUEST_MISMATCH", "worker returned a different request_id")
    if value.get("request_sha256") != request.get("content_sha256"):
        raise ProtocolError("REQUEST_MISMATCH", "worker returned a different request hash")
    status = value.get("status")
    if status not in {"ok", "error"}:
        raise ProtocolError("INVALID_RESPONSE", "status must be ok or error")
    result = value.get("result")
    error = value.get("error")
    if status == "ok":
        if not isinstance(result, Mapping) or error is not None:
            raise ProtocolError("INVALID_RESPONSE", "ok response requires object result and null error")
    else:
        if result is not None or not isinstance(error, Mapping) or set(error) != {"code", "message"}:
            raise ProtocolError("INVALID_RESPONSE", "error response requires exact code/message error")
        if not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            raise ProtocolError("INVALID_RESPONSE", "error code/message must be strings")
    return dict(value)
