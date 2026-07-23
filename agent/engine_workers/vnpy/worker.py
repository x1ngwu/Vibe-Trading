"""vn.py EventEngine and order/trade lifecycle worker for the QE0 PoC only."""

from __future__ import annotations

from datetime import datetime
import hashlib
from importlib.metadata import distribution
from pathlib import Path
from threading import Event as ThreadEvent
import sys
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from worker_runtime import WorkerError, run_worker  # noqa: E402


ENGINE_NAME = "vnpy"
ENGINE_COMMIT = "1b78494979deb4c4996f6b864f234d9839f2f239"
EXPECTED_SOURCE_SHA256 = {
    "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
    "trader_object": "bd360fc224ce22a3f7521bef67ea61125d43c410fa880b9667030ee254546a69",
    "trader_constant": "1361eb485eda9fd97bee3e68324d9b08a96fe927c37a99f8b74de981141e7a0f",
}


def _verify_source() -> dict[str, str]:
    root = Path(distribution("vnpy").locate_file("vnpy")).resolve(strict=True)
    files = {
        "event_engine": root / "event" / "engine.py",
        "trader_object": root / "trader" / "object.py",
        "trader_constant": root / "trader" / "constant.py",
    }
    actual = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in files.items()
    }
    if actual != EXPECTED_SOURCE_SHA256:
        raise WorkerError(
            "ENGINE_PROVENANCE_MISMATCH",
            "installed vn.py source does not match the audited commit",
        )
    return actual


def capabilities(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    try:
        from importlib.metadata import version

        installed_version = version("vnpy")
    except Exception:
        installed_version = None
    return {
        "engine_version": installed_version,
        "engine_commit": ENGINE_COMMIT,
        "operations": {
            "capabilities": "poc",
            "security_probe": "poc",
            "direct_smoke": "poc",
            "normalize_ledger": "not_available_until_qe6",
            "backtest": "not_available_until_qe6",
        },
        "protocol": {"name": "vibe.quant-engine.jsonl", "schema_version": "1.0"},
    }


def direct_smoke(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    try:
        source_sha256 = _verify_source()
        import vnpy
        from vnpy.event import Event, EventEngine
        from vnpy.trader.constant import Direction, Exchange, Status
        from vnpy.trader.object import OrderData, TradeData
    except Exception as exc:
        raise WorkerError("ENGINE_IMPORT_ERROR", f"{type(exc).__name__}: {exc}") from exc

    events: list[dict[str, Any]] = []
    position = 0.0
    completed = ThreadEvent()

    def on_order(event: Event) -> None:
        order = event.data
        events.append({"type": "order", "status": order.status.name, "traded": order.traded})

    def on_trade(event: Event) -> None:
        nonlocal position
        trade = event.data
        position += trade.volume if trade.direction == Direction.LONG else -trade.volume
        events.append({"type": "trade", "tradeid": trade.tradeid, "volume": trade.volume})
        if position == 100:
            completed.set()

    engine = EventEngine(interval=0.01)
    engine.register("eOrder", on_order)
    engine.register("eTrade", on_trade)
    engine.start()
    try:
        now = datetime(2024, 6, 12, 9, 31)
        for status, traded in (
            (Status.NOTTRADED, 0),
            (Status.PARTTRADED, 40),
            (Status.ALLTRADED, 100),
        ):
            engine.put(
                Event(
                    "eOrder",
                    OrderData(
                        gateway_name="QE0",
                        symbol="000001",
                        exchange=Exchange.SZSE,
                        orderid="order-1",
                        direction=Direction.LONG,
                        price=10.0,
                        volume=100,
                        traded=traded,
                        status=status,
                        datetime=now,
                    ),
                )
            )
        for tradeid, volume in (("trade-1", 40), ("trade-2", 60)):
            engine.put(
                Event(
                    "eTrade",
                    TradeData(
                        gateway_name="QE0",
                        symbol="000001",
                        exchange=Exchange.SZSE,
                        orderid="order-1",
                        tradeid=tradeid,
                        direction=Direction.LONG,
                        price=10.0,
                        volume=volume,
                        datetime=now,
                    ),
                )
            )
        if not completed.wait(timeout=2):
            raise WorkerError("ENGINE_SEMANTIC_ERROR", "EventEngine did not deliver the minimal lifecycle")
    finally:
        engine.stop()

    return {
        "import": {
            "version": getattr(vnpy, "__version__", None),
            "source_sha256": source_sha256,
        },
        "event_count": len(events),
        "event_types": [item["type"] for item in events],
        "order_statuses": [item["status"] for item in events if item["type"] == "order"],
        "trade_volumes": [item["volume"] for item in events if item["type"] == "trade"],
        "net_position": position,
    }


if __name__ == "__main__":
    raise SystemExit(
        run_worker(
            engine_name=ENGINE_NAME,
            engine_commit=ENGINE_COMMIT,
            handlers={"capabilities": capabilities, "direct_smoke": direct_smoke},
        )
    )
