"""Strict QE6-1 mapping of one QE5 request into vn.py's EventEngine.

This operation proves that the independent oracle receives the exact QE5
EngineRequest, execution plan, and immutable snapshot identity.  It does not
yet generate orders or an accounting ledger; those semantics belong to
QE6-2/3.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any, Callable, Mapping

from worker_runtime import WorkerError, canonical_json


EVENT_PATH_REQUEST_SCHEMA = "vibe.vnpy-event-path-request.v1"
EVENT_PATH_RESULT_SCHEMA = "vibe.vnpy-event-path-result.v1"
BACKTEST_SNAPSHOT_SCHEMA = "vibe.quantaxis-backtest-snapshot.v1"
QUANTAXIS_ENGINE_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"

_PAYLOAD_KEYS = {
    "schema_version",
    "engine_request",
    "execution_plan",
    "data_snapshot_ref",
}
_ENGINE_REQUEST_KEYS = {
    "object_type",
    "request_id",
    "strategy_spec_ref",
    "data_snapshot_ref",
    "engine",
    "operation",
    "resource_limits",
    "random_seed",
}
_PLAN_KEYS = {
    "schema_version",
    "dsl_version",
    "template_version",
    "template_id",
    "source_mode",
    "plan_id",
    "content_sha256",
    "strategy_spec_ref",
    "data_snapshot_ref",
    "strategy",
    "engine",
    "operation",
    "field_bindings",
    "resource_limits",
    "random_seed",
}
_SNAPSHOT_REF_KEYS = {
    "object_type",
    "snapshot_sha256",
    "manifest_version",
    "as_of",
    "start_date",
    "end_date",
    "frequency",
    "adjustment",
    "symbols",
    "fields",
    "requested_sources",
    "actual_sources",
    "anomalies",
}
_OBJECT_REF_KEYS = {
    "schema_version",
    "object_type",
    "object_id",
    "content_sha256",
}
_SNAPSHOT_KEYS = {
    "schema_version",
    "price_semantics",
    "rule_table",
    "instruments",
    "calendar",
    "bars",
    "corporate_actions",
}
_EVENTS = (
    ("eVibeEngineRequest", "engine_request"),
    ("eVibeExecutionPlan", "execution_plan"),
    ("eVibeDataSnapshot", "data_snapshot"),
)


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            f"{label} keys do not match schema",
        )
    return value


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object_ref(value: Any, label: str) -> Mapping[str, Any]:
    ref = _exact(value, _OBJECT_REF_KEYS, label)
    object_type = ref["object_type"]
    content_sha256 = ref["content_sha256"]
    if (
        ref["schema_version"] != "1.0"
        or not isinstance(object_type, str)
        or not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
        or ref["object_id"] != f"{object_type}:{content_sha256}"
    ):
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            f"{label} identity is invalid",
        )
    return ref


def _load_snapshot(snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if snapshot is None:
        raise WorkerError(
            "SNAPSHOT_REQUIRED",
            "vn.py event replay requires a content-bound snapshot",
        )
    path = Path(str(snapshot["path"]))
    if not path.is_file():
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "vn.py event replay snapshot must be one JSON file",
        )

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "vn.py event replay snapshot is invalid JSON",
        ) from exc
    root = _exact(raw, _SNAPSHOT_KEYS, "backtest snapshot")
    if root["schema_version"] != BACKTEST_SNAPSHOT_SCHEMA:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "unsupported backtest snapshot schema",
        )
    return root


def parse_qe5_identity_payload(
    payload: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    request = _exact(payload, _PAYLOAD_KEYS, "vn.py event path payload")
    if request["schema_version"] != EVENT_PATH_REQUEST_SCHEMA:
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "unsupported vn.py event path request schema",
        )
    engine_request = _exact(
        request["engine_request"],
        _ENGINE_REQUEST_KEYS,
        "EngineRequest",
    )
    plan = _exact(request["execution_plan"], _PLAN_KEYS, "execution plan")
    snapshot_ref = _exact(
        request["data_snapshot_ref"],
        _SNAPSHOT_REF_KEYS,
        "DataSnapshotRef",
    )
    request_snapshot_object_ref = _object_ref(
        engine_request["data_snapshot_ref"],
        "EngineRequest data_snapshot_ref",
    )
    if engine_request["object_type"] != "engine_request":
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "EngineRequest object_type is invalid",
        )
    expected_engine = {
        "name": "quantaxis",
        "commit": QUANTAXIS_ENGINE_COMMIT,
    }
    if engine_request["engine"] != expected_engine or plan["engine"] != expected_engine:
        raise WorkerError(
            "ENGINE_REQUEST_MISMATCH",
            "QE6-1 requires the exact audited QE5 QUANTAXIS EngineRequest",
        )
    if engine_request["operation"] != "backtest" or plan["operation"] != "backtest":
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "QE6-1 only accepts the QE5 backtest operation",
        )
    plan_material = dict(plan)
    plan_sha256 = plan_material.pop("content_sha256", None)
    plan_id = plan_material.pop("plan_id", None)
    expected_plan_sha256 = _canonical_sha256(plan_material)
    if (
        plan_sha256 != expected_plan_sha256
        or plan_id != f"strategy-plan:{expected_plan_sha256}"
    ):
        raise WorkerError(
            "PLAN_IDENTITY_MISMATCH",
            "execution plan content identity is invalid",
        )
    expected_request_id = (
        f"qe4:{plan['template_id']}:{expected_plan_sha256[:24]}"
    )
    if engine_request["request_id"] != expected_request_id:
        raise WorkerError(
            "PLAN_IDENTITY_MISMATCH",
            "EngineRequest request_id does not match the execution plan",
        )
    for key in (
        "strategy_spec_ref",
        "data_snapshot_ref",
        "engine",
        "resource_limits",
        "random_seed",
    ):
        if engine_request[key] != plan[key]:
            raise WorkerError(
                "PLAN_IDENTITY_MISMATCH",
                f"EngineRequest and execution plan disagree on {key}",
            )
    strategy = plan["strategy"]
    if (
        not isinstance(strategy, Mapping)
        or strategy.get("data_snapshot_ref") != plan["data_snapshot_ref"]
    ):
        raise WorkerError(
            "PLAN_IDENTITY_MISMATCH",
            "embedded strategy and execution plan snapshot refs differ",
        )
    if snapshot_ref["object_type"] != "data_snapshot_ref":
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "DataSnapshotRef object_type is invalid",
        )
    # EngineRequest carries the content-addressed ResearchObject ObjectRef,
    # while this field is the referenced DataSnapshotRef payload itself.  The
    # adapter proves that relationship before crossing the worker boundary;
    # here we independently bind the ObjectRef across request/plan/strategy
    # above and bind the payload to the immutable snapshot bytes below.
    if request_snapshot_object_ref["object_type"] != snapshot_ref["object_type"]:
        raise WorkerError(
            "SNAPSHOT_IDENTITY_MISMATCH",
            "DataSnapshotRef type does not match EngineRequest",
        )
    if snapshot is None or snapshot_ref["snapshot_sha256"] != snapshot.get("sha256"):
        raise WorkerError(
            "SNAPSHOT_HASH_MISMATCH",
            "DataSnapshotRef does not match snapshot bytes",
        )
    snapshot_payload = _load_snapshot(snapshot)
    semantics = snapshot_payload["price_semantics"]
    if (
        not isinstance(semantics, Mapping)
        or snapshot_ref["adjustment"]
        != semantics.get("signal_price_adjustment")
    ):
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "DataSnapshotRef adjustment does not match snapshot semantics",
        )
    instruments = snapshot_payload["instruments"]
    if not isinstance(instruments, list) or any(
        not isinstance(item, Mapping) or not isinstance(item.get("symbol"), str)
        for item in instruments
    ):
        raise WorkerError(
            "INVALID_OPERATION_INPUT",
            "snapshot instruments are invalid",
        )
    symbols = tuple(item["symbol"] for item in instruments)
    if tuple(snapshot_ref["symbols"]) != symbols or len(symbols) != len(set(symbols)):
        raise WorkerError(
            "SNAPSHOT_SEMANTICS_MISMATCH",
            "DataSnapshotRef symbols do not match snapshot instruments",
        )
    return {
        "engine_request": engine_request,
        "plan": plan,
        "snapshot_ref": snapshot_ref,
        "engine_request_sha256": _canonical_sha256(engine_request),
        "execution_plan_sha256": expected_plan_sha256,
        "data_snapshot_ref_sha256": _canonical_sha256(snapshot_ref),
        "event_path_input_sha256": _canonical_sha256(request),
        "snapshot_sha256": str(snapshot["sha256"]),
    }


def _event_replay(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    load_boundary: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any]:
    parsed = parse_qe5_identity_payload(payload, snapshot=snapshot)
    try:
        boundary = load_boundary()
        event_type = boundary["Event"]
        event_engine_type = boundary["EventEngine"]
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(
            "ENGINE_IMPORT_ERROR",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    expected_hashes = {
        "engine_request": parsed["engine_request_sha256"],
        "execution_plan": parsed["execution_plan_sha256"],
        "data_snapshot": parsed["data_snapshot_ref_sha256"],
    }
    events: list[dict[str, str]] = []
    completed = ThreadEvent()

    def handler(event: Any) -> None:
        data = event.data
        if not isinstance(data, Mapping):
            return
        events.append(
            {
                "kind": str(data.get("kind")),
                "payload_sha256": str(data.get("payload_sha256")),
            }
        )
        if len(events) == len(_EVENTS):
            completed.set()

    engine = event_engine_type(interval=0.01)
    for vnpy_type, _ in _EVENTS:
        engine.register(vnpy_type, handler)
    engine.start()
    try:
        for vnpy_type, kind in _EVENTS:
            engine.put(
                event_type(
                    vnpy_type,
                    {
                        "kind": kind,
                        "payload_sha256": expected_hashes[kind],
                    },
                )
            )
        if not completed.wait(timeout=2):
            raise WorkerError(
                "ENGINE_SEMANTIC_ERROR",
                "vn.py EventEngine did not deliver the QE6-1 identity path",
            )
    finally:
        engine.stop()
    expected_events = [
        {"kind": kind, "payload_sha256": expected_hashes[kind]}
        for _, kind in _EVENTS
    ]
    if events != expected_events:
        raise WorkerError(
            "ENGINE_SEMANTIC_ERROR",
            "vn.py EventEngine changed the QE6-1 identity event sequence",
        )
    return {
        "operation_schema": EVENT_PATH_RESULT_SCHEMA,
        "snapshot_sha256": parsed["snapshot_sha256"],
        "engine_request_id": parsed["engine_request"]["request_id"],
        "engine_request_sha256": parsed["engine_request_sha256"],
        "execution_plan_sha256": parsed["execution_plan_sha256"],
        "event_path_input_sha256": parsed["event_path_input_sha256"],
        "events": events,
        "engine_version": boundary["version"],
        "source_sha256": boundary["source_sha256"],
    }


def build_event_replay_handler(
    load_boundary: Callable[[], Mapping[str, Any]],
) -> Callable[[Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]]:
    return lambda payload, snapshot: _event_replay(
        payload,
        snapshot,
        load_boundary,
    )
