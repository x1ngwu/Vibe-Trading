"""QE5-5 Agent tool for submitting one exact confirmed strategy backtest."""

from __future__ import annotations

from typing import Any

from src.agent.tools import BaseTool
from src.quant_engine.product import (
    BacktestProductService,
    default_backtest_product_service,
    persist_backtest_visualization,
)
from src.research.contracts import canonical_json
from src.tools.path_utils import safe_run_dir


_HOUSEHOLD_INITIAL_CASH_FEN = 100_000_000


class StrategyBacktestTool(BaseTool):
    """Submit a confirmed version to the persistent bounded QE5 runtime."""

    name = "run_strategy_backtest"
    description = (
        "Run the exact current confirmed StrategyVersion through the governed "
        "QUANTAXIS backtest runtime. Supply only its canonical strategy_version_id "
        "and a fresh idempotency key. The tool never accepts Python, order-routing, "
        "broker, snapshot-path, engine, or arbitrary strategy inputs. It returns a "
        "persistent job/result visualization that survives refresh and can be "
        "cancelled through the authenticated API."
    )
    parameters = {
        "type": "object",
        "properties": {
            "strategy_version_id": {
                "type": "string",
                "pattern": r"^strategy-version:[0-9a-f]{64}$",
            },
            "idempotency_key": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            },
        },
        "required": ["strategy_version_id", "idempotency_key"],
        "additionalProperties": False,
    }
    repeatable = True
    is_readonly = False
    requires_current_run_dir = True

    def __init__(
        self,
        *,
        default_session_id: str | None = None,
        event_callback=None,
        service: BacktestProductService | None = None,
    ) -> None:
        self._session_id = default_session_id
        self._event_callback = event_callback
        self._service = service

    def execute(self, **kwargs: Any) -> str:
        if not self._session_id:
            raise ValueError("run_strategy_backtest requires an injected session identity")
        run_dir = safe_run_dir(str(kwargs.get("run_dir") or ""))
        service = self._service or default_backtest_product_service()
        submitted, spec = service.submit(
            session_id=self._session_id,
            strategy_version_id=str(kwargs.get("strategy_version_id") or ""),
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
            initial_cash_fen=_HOUSEHOLD_INITIAL_CASH_FEN,
        )
        persist_backtest_visualization(run_dir, spec)
        if self._event_callback is not None:
            self._event_callback(
                "backtest.submitted",
                {
                    "job_id": submitted.job.job_id,
                    "strategy_version_id": spec.strategy_version_id,
                    "status": submitted.job.status,
                    "created": submitted.created,
                },
            )
        return canonical_json(
            {
                "status": submitted.job.status,
                "job_id": submitted.job.job_id,
                "created": submitted.created,
                "strategy_version_id": spec.strategy_version_id,
                "initial_cash_fen": _HOUSEHOLD_INITIAL_CASH_FEN,
                "visualizations": [spec.model_dump(mode="json")],
                "live_trading": False,
            }
        )
