"""Authenticated QE4 strategy confirmation routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.strategy_spec.presentation import (
    StrategyConfirmationVisualizationPayload,
    build_strategy_visualization,
    default_strategy_version_db_path,
    persist_strategy_visualization,
)
from src.strategy_spec.version_store import (
    StrategyVersionStore,
    StrategyVersionStoreIntegrityError,
)
from src.strategy_spec.versioning import (
    ExpiredStrategyConfirmationError,
    StaleStrategyHeadError,
    StrategyConfirmationHashMismatch,
    StrategyHeadToken,
    StrategyIdempotencyConflict,
    StrategyVersionError,
)


class ConfirmStrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_head: StrategyHeadToken
    confirmation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )


def register_strategy_routes(app: FastAPI) -> None:
    """Mount exact-card confirmation without exposing any worker operation."""

    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError("strategy routes require the assembled api_server module")
    require_auth = host.require_auth

    def _validate(value: str, kind: str) -> None:
        current = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        current._validate_path_param(value, kind)

    def _runs_dir() -> Path:
        current = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return current.RUNS_DIR

    def _require_session(session_id: str) -> None:
        current = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        service = current._get_session_service()
        if service is None:
            raise HTTPException(status_code=501, detail="Session runtime not enabled")
        if service.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")

    @app.post(
        "/sessions/{session_id}/runs/{run_id}/strategy-confirmations/"
        "{visualization_id}/confirm",
        dependencies=[Depends(require_auth)],
    )
    async def confirm_strategy_card(
        session_id: str,
        run_id: str,
        visualization_id: str,
        request: ConfirmStrategyRequest,
    ):
        _validate(session_id, "session_id")
        _validate(run_id, "run_id")
        _validate(visualization_id, "visualization_id")
        _require_session(session_id)
        path = (
            _runs_dir()
            / run_id
            / "artifacts"
            / "visualizations"
            / f"{visualization_id}.json"
        )
        try:
            root = _runs_dir().resolve()
            resolved = path.resolve(strict=True)
            path_chain = (
                path.parent.parent.parent,
                path.parent.parent,
                path.parent,
                path,
            )
            if (
                root not in resolved.parents
                or any(item.is_symlink() for item in path_chain)
                or not path.is_file()
                or path.stat().st_size > 5_000_000
            ):
                raise HTTPException(status_code=404, detail="strategy card not found")
            stored = StrategyConfirmationVisualizationPayload.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except HTTPException:
            raise
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="strategy card not found")
        except (OSError, json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid strategy card artifact")
        if (
            stored.visualization_id != visualization_id
            or stored.stream_id != session_id
            or request.expected_head.stream_id != session_id
            or request.expected_head.version_id != stored.version.version_id
            or stored.card is None
            or stored.card.confirmation_hash != request.confirmation_hash
        ):
            raise HTTPException(
                status_code=409,
                detail="strategy card does not match this session/version/head",
            )

        try:
            with StrategyVersionStore(default_strategy_version_db_path()) as store:
                receipt = store.confirm(
                    stream_id=session_id,
                    expected_head=request.expected_head,
                    confirmation_hash=request.confirmation_hash,
                    idempotency_key=request.idempotency_key,
                    actor_id="household-user",
                )
                version = store.get_version(receipt.version_id)
                head = store.get_head(session_id)
                card = store.get_confirmation_card(receipt.confirmation_hash)
                if version is None or head is None or card is None:
                    raise ValueError("confirmed strategy chain is incomplete")
                spec, payload = build_strategy_visualization(
                    version=version,
                    head=head,
                    card=card,
                    receipt=receipt,
                    data_basis=stored.data_basis,
                    head_confirmation_hash=receipt.confirmation_hash,
                )
                persist_strategy_visualization(path.parents[2], spec, payload)
        except ExpiredStrategyConfirmationError as exc:
            raise HTTPException(status_code=410, detail=str(exc))
        except (
            StaleStrategyHeadError,
            StrategyConfirmationHashMismatch,
            StrategyIdempotencyConflict,
            StrategyVersionError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except StrategyVersionStoreIntegrityError:
            raise HTTPException(status_code=422, detail="strategy version store integrity failure")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return JSONResponse(payload.model_dump(mode="json"))
