"""Pinned vn.py worker for the QE0 smoke and QE6 independent oracle."""

from __future__ import annotations

from datetime import datetime
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from threading import Event as ThreadEvent
import sys
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from worker_runtime import WorkerError, run_worker  # noqa: E402
from formal_event_path import build_event_replay_handler  # noqa: E402


ENGINE_NAME = "vnpy"
ENGINE_COMMIT = "1b78494979deb4c4996f6b864f234d9839f2f239"
EXPECTED_ENGINE_VERSION = "4.4.0"
EXPECTED_SOURCE_SHA256 = {
    "package_init": "ba16287a3acd984a6e68c3e373441c7e8af623ad9df74837227368a4456915a8",
    "event_init": "81752eb9db5a9e9bdf7024f9a8821cf569c5994eceff9194a6ecd725dc8ed365",
    "event_engine": "079c76f3c99ed4dc1e28dd0ba9a30991f41bfd613c9ab30b1fa0ea6f1da6b76b",
    "trader_init": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "trader_locale_init": "3138d59a9d7bf99cdcc683778e402f0dd7fabcf9698c3ec2820e44508ac0661c",
    "trader_object": "bd360fc224ce22a3f7521bef67ea61125d43c410fa880b9667030ee254546a69",
    "trader_constant": "1361eb485eda9fd97bee3e68324d9b08a96fe927c37a99f8b74de981141e7a0f",
}


SOURCE_RELATIVE_PATHS = {
    "package_init": ("__init__.py",),
    "event_init": ("event", "__init__.py"),
    "event_engine": ("event", "engine.py"),
    "trader_init": ("trader", "__init__.py"),
    "trader_locale_init": ("trader", "locale", "__init__.py"),
    "trader_object": ("trader", "object.py"),
    "trader_constant": ("trader", "constant.py"),
}


def _verify_installation() -> tuple[str, dict[str, str]]:
    """Require the exact package version and every imported upstream source file."""

    try:
        dist = distribution("vnpy")
    except PackageNotFoundError as exc:
        raise WorkerError(
            "ENGINE_UNAVAILABLE",
            "the pinned vn.py distribution is not installed",
        ) from exc

    installed_version = dist.version
    if installed_version != EXPECTED_ENGINE_VERSION:
        raise WorkerError(
            "ENGINE_VERSION_MISMATCH",
            f"expected vn.py {EXPECTED_ENGINE_VERSION}, got {installed_version}",
        )

    try:
        root = Path(dist.locate_file("vnpy")).resolve(strict=True)
        files = {
            name: root.joinpath(*relative)
            for name, relative in SOURCE_RELATIVE_PATHS.items()
        }
        if any(path.is_symlink() or not path.is_file() for path in files.values()):
            raise OSError("audited source is missing or is a symlink")
        actual = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in files.items()
        }
    except OSError as exc:
        raise WorkerError(
            "ENGINE_PROVENANCE_MISMATCH",
            "installed vn.py source closure is incomplete",
        ) from exc
    if actual != EXPECTED_SOURCE_SHA256:
        raise WorkerError(
            "ENGINE_PROVENANCE_MISMATCH",
            "installed vn.py source does not match the audited commit",
        )
    return installed_version, actual


def capabilities(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    installed_version, source_sha256 = _verify_installation()
    return {
        "engine_version": installed_version,
        "engine_commit": ENGINE_COMMIT,
        "source_sha256": source_sha256,
        "operations": {
            "capabilities": "poc",
            "security_probe": "poc",
            "direct_smoke": "poc",
            "event_replay": "qe6_1",
            "normalize_ledger": "not_available_until_qe6_2",
            "backtest": "not_available",
        },
        "protocol": {"name": "vibe.quant-engine.jsonl", "schema_version": "1.0"},
    }


def _load_vnpy_event_boundary() -> Mapping[str, Any]:
    """Load only the audited EventEngine boundary used by QE6-1."""

    installed_version, source_sha256 = _verify_installation()
    try:
        from vnpy.event import Event, EventEngine
    except Exception as exc:
        raise WorkerError(
            "ENGINE_IMPORT_ERROR",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return {
        "version": installed_version,
        "source_sha256": source_sha256,
        "Event": Event,
        "EventEngine": EventEngine,
    }


def direct_smoke(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    try:
        boundary = _load_vnpy_event_boundary()
        import vnpy
        from vnpy.trader.constant import Direction, Exchange, Status
        from vnpy.trader.object import OrderData, TradeData
    except WorkerError:
        raise
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

    event_type = boundary["Event"]
    engine = boundary["EventEngine"](interval=0.01)
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
                event_type(
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
                event_type(
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
            "source_sha256": boundary["source_sha256"],
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
            handlers={
                "capabilities": capabilities,
                "direct_smoke": direct_smoke,
                "event_replay": build_event_replay_handler(
                    _load_vnpy_event_boundary
                ),
            },
        )
    )
