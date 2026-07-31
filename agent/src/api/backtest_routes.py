"""Authenticated QE5-5 submit/status/cancel/SSE/history/compare routes."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.quant_engine import (
    BacktestBackpressureError,
    BacktestJobConflictError,
)
from src.quant_engine.product import (
    BacktestProductError,
    BacktestProductService,
    default_backtest_product_service,
)


_JOB_ID = re.compile(r"^backtest-job:[0-9a-f]{64}$")
_backtest_service: BacktestProductService | None = None


class SubmitBacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_version_id: str = Field(pattern=r"^strategy-version:[0-9a-f]{64}$")
    idempotency_key: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    initial_cash_fen: int = Field(default=100_000_000, gt=0)


class CompareBacktestsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_job_id: str = Field(pattern=_JOB_ID.pattern)
    right_job_id: str = Field(pattern=_JOB_ID.pattern)


def _get_backtest_service() -> BacktestProductService:
    return _backtest_service or default_backtest_product_service()


def _sse(event: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.extend(
        (
            f"event: {event}",
            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}",
            "",
            "",
        )
    )
    return "\n".join(lines)


def register_backtest_routes(app: FastAPI) -> None:
    """Mount QE5 product routes without exposing engine or snapshot arguments."""

    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError("backtest routes require the assembled api_server module")
    require_auth = host.require_auth
    require_event_stream_auth = host.require_event_stream_auth

    def validate_session(session_id: str) -> None:
        current = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        current._validate_path_param(session_id, "session_id")
        session_service = current._get_session_service()
        if session_service is None:
            raise HTTPException(status_code=501, detail="Session runtime not enabled")
        if session_service.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")

    def validate_job(job_id: str) -> None:
        if not _JOB_ID.fullmatch(job_id):
            raise HTTPException(status_code=400, detail="invalid backtest job id")

    def service() -> BacktestProductService:
        try:
            return _get_backtest_service()
        except BacktestProductError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    @app.post(
        "/sessions/{session_id}/backtests",
        dependencies=[Depends(require_auth)],
    )
    async def submit_backtest(session_id: str, body: SubmitBacktestRequest):
        validate_session(session_id)
        try:
            submitted, spec = service().submit(
                session_id=session_id,
                strategy_version_id=body.strategy_version_id,
                idempotency_key=body.idempotency_key,
                initial_cash_fen=body.initial_cash_fen,
            )
        except BacktestBackpressureError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        except BacktestJobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except BacktestProductError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {
            "created": submitted.created,
            "job": submitted.job.model_dump(mode="json"),
            "visualization": spec.model_dump(mode="json"),
        }

    @app.get(
        "/sessions/{session_id}/backtests",
        dependencies=[Depends(require_auth)],
    )
    async def list_backtests(session_id: str, limit: int = 100):
        validate_session(session_id)
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        return [
            item.model_dump(mode="json")
            for item in service().list_jobs(session_id=session_id, limit=limit)
        ]

    @app.get(
        "/sessions/{session_id}/backtests/{job_id}",
        dependencies=[Depends(require_auth)],
    )
    async def get_backtest(session_id: str, job_id: str):
        validate_session(session_id)
        validate_job(job_id)
        try:
            payload = service().payload(session_id=session_id, job_id=job_id)
        except BacktestProductError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if payload is None:
            raise HTTPException(status_code=404, detail="backtest job not found")
        return payload.model_dump(mode="json")

    @app.post(
        "/sessions/{session_id}/backtests/{job_id}/cancel",
        dependencies=[Depends(require_auth)],
    )
    async def cancel_backtest(session_id: str, job_id: str):
        validate_session(session_id)
        validate_job(job_id)
        job = service().cancel(session_id=session_id, job_id=job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="backtest job not found")
        return job.model_dump(mode="json")

    @app.post(
        "/sessions/{session_id}/backtests/compare",
        dependencies=[Depends(require_auth)],
    )
    async def compare_backtests(session_id: str, body: CompareBacktestsRequest):
        validate_session(session_id)
        try:
            return service().compare(
                session_id=session_id,
                left_job_id=body.left_job_id,
                right_job_id=body.right_job_id,
            ).model_dump(mode="json")
        except BacktestProductError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get(
        "/sessions/{session_id}/backtests/{job_id}/events",
        dependencies=[Depends(require_event_stream_auth)],
    )
    async def stream_backtest(
        session_id: str,
        job_id: str,
        request: Request,
    ) -> StreamingResponse:
        validate_session(session_id)
        validate_job(job_id)
        if service().get_job(session_id=session_id, job_id=job_id) is None:
            raise HTTPException(status_code=404, detail="backtest job not found")

        async def event_stream():
            last_payload = ""
            last_emit = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = service().payload(
                        session_id=session_id,
                        job_id=job_id,
                    )
                except BacktestProductError as exc:
                    yield _sse("error", {"message": str(exc)})
                    yield _sse("done", {"job_id": job_id})
                    return
                if payload is None:
                    yield _sse("error", {"message": "backtest job vanished"})
                    yield _sse("done", {"job_id": job_id})
                    return
                data = payload.model_dump(mode="json")
                encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
                if encoded != last_payload:
                    event_id = f"{payload.status}:{payload.job_id[-12:]}"
                    yield _sse("backtest", data, event_id=event_id)
                    last_payload = encoded
                    last_emit = time.monotonic()
                if payload.status in {"completed", "failed", "cancelled"}:
                    yield _sse(
                        "done",
                        {"job_id": job_id, "status": payload.status},
                    )
                    return
                if time.monotonic() - last_emit >= 15:
                    yield ": ping\n\n"
                    last_emit = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
