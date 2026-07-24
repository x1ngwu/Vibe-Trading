"""Typed main-process boundary for the QE2 QUANTAXIS base operations.

This adapter is intentionally not registered with the production backtest
runner yet.  It binds every call to the exact engine commit and exact snapshot
bytes, while the isolated worker owns operation-specific semantic validation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .runner import RunResult, WorkerRunner, compute_snapshot_sha256


QUANTAXIS_ENGINE_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QuantaxisFactorSpec(_StrictModel):
    name: Literal["ma", "ema"]
    window: int = Field(ge=2, le=512)


class QuantaxisOperationError(RuntimeError):
    """Stable error returned by a validated but unsuccessful worker call."""

    def __init__(self, code: str, message: str, *, run: RunResult) -> None:
        super().__init__(message)
        self.code = code
        self.run = run


class QuantaxisAdapter:
    """Small QE2 facade over one exact :class:`WorkerRunner` instance."""

    def __init__(self, runner: WorkerRunner) -> None:
        if runner.config.engine.name != "quantaxis":
            raise ValueError("QuantaxisAdapter requires the quantaxis engine")
        if runner.config.engine.commit != QUANTAXIS_ENGINE_COMMIT:
            raise ValueError("QuantaxisAdapter requires the audited QUANTAXIS commit")
        self.runner = runner

    def _run(
        self,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, Any],
        snapshot_path: Path,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        path = snapshot_path.absolute()
        snapshot_sha256 = compute_snapshot_sha256(path)
        run = self.runner.run(
            request_id=request_id,
            operation=operation,
            payload=payload,
            snapshot_path=str(path),
            snapshot_sha256=snapshot_sha256,
            timeout_seconds=timeout_seconds,
        )
        response = run.response
        if response["status"] != "ok":
            error = response["error"]
            raise QuantaxisOperationError(error["code"], error["message"], run=run)
        result = response["result"]
        if result.get("snapshot_sha256") != snapshot_sha256:
            raise QuantaxisOperationError(
                "SNAPSHOT_RESULT_MISMATCH",
                "worker result did not bind the input snapshot hash",
                run=run,
            )
        return result

    def adjust_prices(
        self,
        *,
        request_id: str,
        symbol: str,
        adjustment: Literal["qfq", "hfq"],
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        return self._run(
            request_id=request_id,
            operation="adjust_prices",
            payload={"symbol": symbol, "adjustment": adjustment},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )

    def trading_calendar(
        self,
        *,
        request_id: str,
        start_date: date,
        end_date: date,
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if start_date > end_date:
            raise ValueError("start_date must not follow end_date")
        return self._run(
            request_id=request_id,
            operation="trading_calendar",
            payload={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )

    def compute_factors(
        self,
        *,
        request_id: str,
        symbol: str,
        factors: tuple[QuantaxisFactorSpec, ...],
        snapshot_path: Path,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if not factors or len(factors) > 16:
            raise ValueError("factors must contain 1 to 16 specifications")
        identities = [(item.name, item.window) for item in factors]
        if len(set(identities)) != len(identities):
            raise ValueError("factor specifications must be unique")
        return self._run(
            request_id=request_id,
            operation="compute_factors",
            payload={"symbol": symbol, "factors": [item.model_dump() for item in factors]},
            snapshot_path=snapshot_path,
            timeout_seconds=timeout_seconds,
        )
