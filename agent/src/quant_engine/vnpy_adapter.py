"""QE6-1 boundary for replaying the exact QE5 request through vn.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    DataSnapshotRef,
    EngineRequest,
    ResearchObject,
    canonical_sha256,
)
from src.strategy_spec.compiler import StrategyCompilation

from .quantaxis_adapter import QUANTAXIS_ENGINE_COMMIT
from .runner import RunResult, WorkerRunner, compute_snapshot_sha256


VNPY_ENGINE_COMMIT = "1b78494979deb4c4996f6b864f234d9839f2f239"
VNPY_ENGINE_VERSION = "4.4.0"
VNPY_SOURCE_SHA256 = {
    "package_init": "ba16287a3acd984a6e68c3e373441c7e8af623ad9df74837227368a4456915a8",
    "event_init": "81752eb9db5a9e9bdf7024f9a8821cf569c5994eceff9194a6ecd725dc8ed365",
    "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
    "trader_init": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "trader_locale_init": "3138d59a9d7bf99cdcc683778e402f0dd7fabcf9698c3ec2820e44508ac0661c",
    "trader_object": "bd360fc224ce22a3f7521bef67ea61125d43c410fa880b9667030ee254546a69",
    "trader_constant": "1361eb485eda9fd97bee3e68324d9b08a96fe927c37a99f8b74de981141e7a0f",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VnpyIdentityEvent(_StrictModel):
    kind: Literal["engine_request", "execution_plan", "data_snapshot"]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VnpyEventPathResult(_StrictModel):
    operation_schema: Literal[
        "vibe.vnpy-event-path-result.v1"
    ] = "vibe.vnpy-event-path-result.v1"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_request_id: str
    engine_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_path_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: tuple[VnpyIdentityEvent, ...]
    engine_version: str
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_events(self) -> "VnpyEventPathResult":
        if tuple(event.kind for event in self.events) != (
            "engine_request",
            "execution_plan",
            "data_snapshot",
        ):
            raise ValueError("vn.py identity events are incomplete or unordered")
        return self


@dataclass(frozen=True)
class VnpyEventReplay:
    """Validated QE6-1 EventEngine identity receipt."""

    worker: VnpyEventPathResult
    qe5_engine_request: EngineRequest


class VnpyOperationError(RuntimeError):
    """Stable error returned by an unsuccessful vn.py worker call."""

    def __init__(self, code: str, message: str, *, run: RunResult) -> None:
        super().__init__(message)
        self.code = code
        self.run = run


class VnpyOracleAdapter:
    """Map one exact QE5 compilation into the independent vn.py worker."""

    def __init__(self, runner: WorkerRunner) -> None:
        if runner.config.engine.name != "vnpy":
            raise ValueError("VnpyOracleAdapter requires the vnpy engine")
        if runner.config.engine.commit != VNPY_ENGINE_COMMIT:
            raise ValueError("VnpyOracleAdapter requires the audited vn.py commit")
        self.runner = runner

    def _run(
        self,
        *,
        request_id: str,
        payload: Mapping[str, Any],
        snapshot_path: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        memory_bytes: int,
    ) -> Mapping[str, Any]:
        path = snapshot_path.absolute()
        snapshot_sha256 = compute_snapshot_sha256(path)
        run = self.runner.run(
            request_id=request_id,
            operation="event_replay",
            payload=payload,
            snapshot_path=str(path),
            snapshot_sha256=snapshot_sha256,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            memory_bytes=memory_bytes,
            max_open_files=256,
        )
        response = run.response
        if response["status"] != "ok":
            error = response["error"]
            raise VnpyOperationError(error["code"], error["message"], run=run)
        result = response["result"]
        if result.get("snapshot_sha256") != snapshot_sha256:
            raise VnpyOperationError(
                "SNAPSHOT_RESULT_MISMATCH",
                "vn.py result did not bind the input snapshot hash",
                run=run,
            )
        return result

    def replay_event_path(
        self,
        *,
        compilation: StrategyCompilation,
        snapshot: ResearchObject,
        snapshot_path: Path,
    ) -> VnpyEventReplay:
        """Replay the exact QE5 identities without generating a second ledger."""

        request = compilation.engine_request.payload
        if not isinstance(request, EngineRequest):
            raise TypeError("compilation does not contain EngineRequest")
        if (
            request.engine.name != "quantaxis"
            or request.engine.commit != QUANTAXIS_ENGINE_COMMIT
        ):
            raise ValueError(
                "QE6-1 requires the exact audited QE5 QUANTAXIS EngineRequest"
            )
        if snapshot.ref() != compilation.plan.data_snapshot_ref:
            raise ValueError("execution plan does not reference the supplied snapshot")
        if not isinstance(snapshot.payload, DataSnapshotRef):
            raise TypeError("snapshot must contain DataSnapshotRef")
        actual_snapshot_sha256 = compute_snapshot_sha256(snapshot_path.absolute())
        if snapshot.payload.snapshot_sha256 != actual_snapshot_sha256:
            raise ValueError("DataSnapshotRef does not bind the supplied snapshot bytes")
        worker_payload = {
            "schema_version": "vibe.vnpy-event-path-request.v1",
            "engine_request": request.model_dump(mode="json"),
            "execution_plan": compilation.plan.model_dump(mode="json"),
            "data_snapshot_ref": snapshot.payload.model_dump(mode="json"),
        }
        raw = self._run(
            request_id=f"qe6:{request.request_id}",
            payload=worker_payload,
            snapshot_path=snapshot_path,
            timeout_seconds=request.resource_limits.timeout_seconds,
            max_stdout_bytes=request.resource_limits.max_stdout_bytes,
            max_stderr_bytes=request.resource_limits.max_stderr_bytes,
            memory_bytes=request.resource_limits.memory_bytes,
        )
        result = VnpyEventPathResult.model_validate(raw)
        expected_events = (
            VnpyIdentityEvent(
                kind="engine_request",
                payload_sha256=canonical_sha256(request),
            ),
            VnpyIdentityEvent(
                kind="execution_plan",
                payload_sha256=compilation.plan.content_sha256,
            ),
            VnpyIdentityEvent(
                kind="data_snapshot",
                payload_sha256=canonical_sha256(snapshot.payload),
            ),
        )
        if (
            result.snapshot_sha256 != actual_snapshot_sha256
            or result.engine_request_id != request.request_id
            or result.engine_request_sha256 != canonical_sha256(request)
            or result.execution_plan_sha256 != compilation.plan.content_sha256
            or result.event_path_input_sha256 != canonical_sha256(worker_payload)
            or result.events != expected_events
        ):
            raise ValueError("vn.py event path identity does not match QE5 inputs")
        if (
            result.engine_version != VNPY_ENGINE_VERSION
            or result.source_sha256 != VNPY_SOURCE_SHA256
        ):
            raise ValueError("worker engine provenance does not match audited vn.py")
        return VnpyEventReplay(worker=result, qe5_engine_request=request)
