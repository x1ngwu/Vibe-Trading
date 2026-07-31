"""Pinned, offline QUANTAXIS worker for the QE0 PoC and QE2 base operations."""

from __future__ import annotations

import hashlib
import importlib.util
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from worker_runtime import WorkerError, run_worker  # noqa: E402
from formal_operations import build_formal_handlers  # noqa: E402
from formal_backtest import build_backtest_handler  # noqa: E402


ENGINE_NAME = "quantaxis"
ENGINE_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"
EXPECTED_ENGINE_VERSION = "2.1.0a2"
EXPECTED_SOURCE_SHA256 = {
    "data_fq": "8ea6b152a4eff20bffba2216ae8dc88edbb3f0f11b0a220abb19c574cf459eff",
    "indicator_base": "fbbb3debfa0d061eb4d6e56acf783dea18a144f56ffc191769df075cf38c4737",
    "indicators": "94995068dbe73fdeddfea69bd51c0df52af6fc4567c5c432a265c2e3f9363ff5",
    "calendar": "014ff7173c349da78bbf60dcad52227881d9f0a32d958d98973c4914e12acf45",
    "market_preset": "d789ed62fd173ce2102f87c22ec6e7c155f6c07180e383ff49965fbed70bf8fd",
    "position": "531b691e8a8791cf1a5e9136a980f5a6972e2e4f6fd78df994766213f80dda55",
    "qifi_account": "8b1cae0450d7c3cf9fb4f112191724096f09c84fd6d9c11662d2f13020166a9d",
    "parameters": "dfb09865c6d6016cfea5c2a6009b09353fbe6aa5416a2df53ede82d2ca8ab773",
}
SOURCE_RELATIVE_PATHS = {
    "data_fq": ("QAData", "data_fq.py"),
    "indicator_base": ("QAIndicator", "base.py"),
    "indicators": ("QAIndicator", "indicators.py"),
    "calendar": ("QAUtil", "QADate_trade.py"),
    "market_preset": ("QAMarket", "market_preset.py"),
    "position": ("QAMarket", "QAPosition.py"),
    "qifi_account": ("QIFI", "QifiAccount.py"),
    "parameters": ("QAUtil", "QAParameter.py"),
}


def _namespace(name: str, path: Path) -> ModuleType:
    """Install a namespace shell without executing QUANTAXIS package initializers."""

    module = ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _stub(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_source(name: str, path: Path) -> ModuleType:
    """Load one audited upstream source file while bypassing its broad package init."""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise WorkerError("ENGINE_IMPORT_ERROR", f"cannot load upstream module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _source_files(root: Path) -> dict[str, Path]:
    return {
        name: root.joinpath(*relative)
        for name, relative in SOURCE_RELATIVE_PATHS.items()
    }


def _verify_installation() -> tuple[Path, str, dict[str, str]]:
    """Require the exact package version and every audited executable source file."""

    try:
        dist = distribution("quantaxis")
    except PackageNotFoundError as exc:
        raise WorkerError(
            "ENGINE_UNAVAILABLE",
            "the pinned QUANTAXIS distribution is not installed",
        ) from exc

    installed_version = dist.version
    if installed_version != EXPECTED_ENGINE_VERSION:
        raise WorkerError(
            "ENGINE_VERSION_MISMATCH",
            f"expected QUANTAXIS {EXPECTED_ENGINE_VERSION}, got {installed_version}",
        )

    try:
        root = Path(dist.locate_file("QUANTAXIS")).resolve(strict=True)
        if not (root / "__init__.py").is_file():
            raise OSError("invalid package root")
        source_files = _source_files(root)
        if any(path.is_symlink() or not path.is_file() for path in source_files.values()):
            raise OSError("audited source is missing or is a symlink")
        actual = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in source_files.items()
        }
    except OSError as exc:
        raise WorkerError(
            "ENGINE_PROVENANCE_MISMATCH",
            "installed QUANTAXIS source closure is incomplete",
        ) from exc
    if actual != EXPECTED_SOURCE_SHA256:
        raise WorkerError(
            "ENGINE_PROVENANCE_MISMATCH",
            "installed QUANTAXIS source does not match the audited commit",
        )
    return root, installed_version, actual


def _load_quantaxis_base_boundary() -> dict[str, Any]:
    """Load only the audited leaves executed by the QE2 base operations."""

    root, installed_version, source_sha256 = _verify_installation()
    for name, relative in (
        ("QUANTAXIS", "."),
        ("QUANTAXIS.QAUtil", "QAUtil"),
        ("QUANTAXIS.QAData", "QAData"),
        ("QUANTAXIS.QAIndicator", "QAIndicator"),
    ):
        _namespace(name, root / relative)

    parameters = _load_source(
        "QUANTAXIS.QAUtil.QAParameter", root / "QAUtil" / "QAParameter.py"
    )
    util = sys.modules["QUANTAXIS.QAUtil"]
    util.DATABASE = None
    util.QA_util_log_info = lambda *args, **kwargs: None
    data_fq = _load_source("QUANTAXIS.QAData.data_fq", root / "QAData" / "data_fq.py")
    indicator_base = _load_source(
        "QUANTAXIS.QAIndicator.base", root / "QAIndicator" / "base.py"
    )
    indicators = _load_source(
        "QUANTAXIS.QAIndicator.indicators", root / "QAIndicator" / "indicators.py"
    )
    calendar = _load_source(
        "QUANTAXIS.QAUtil.QADate_trade", root / "QAUtil" / "QADate_trade.py"
    )
    executed = ("data_fq", "indicator_base", "indicators", "calendar", "parameters")
    return {
        "version": installed_version,
        "data_fq": data_fq,
        "indicators": indicators,
        "indicator_base": indicator_base,
        "calendar": calendar,
        "parameters": parameters,
        "source_sha256": {name: source_sha256[name] for name in executed},
    }


def _load_quantaxis_backtest_boundary() -> dict[str, Any]:
    """Load only the deterministic leaves used by the formal backtest.

    QIFI imports MongoDB and ClickHouse clients even for its nominally offline
    account mode.  It was useful as a QE0 smoke candidate, but it is not an
    independent accounting oracle and its state was never trusted by QE5.
    The formal path therefore executes the audited indicator leaves while the
    Vibe integer-fen ledger remains the execution/accounting authority.

    Installation verification still covers the complete eight-file audited
    closure, including QIFI, so a different or partially modified QUANTAXIS
    wheel cannot be substituted silently. Provenance output remains limited to
    the five leaves actually loaded by this operation.
    """

    # _load_quantaxis_base_boundary verifies the complete eight-file closure
    # before importing pandas/numpy. Do not rescan package metadata after those
    # shared objects consume virtual address space under RLIMIT_AS.
    return _load_quantaxis_base_boundary()


def _load_quantaxis_boundary() -> dict[str, Any]:
    """Load the broader audited boundary retained for the QE0 direct smoke."""

    root, installed_version, source_sha256 = _verify_installation()

    for name, relative in (
        ("QUANTAXIS", "."),
        ("QUANTAXIS.QAUtil", "QAUtil"),
        ("QUANTAXIS.QAData", "QAData"),
        ("QUANTAXIS.QAIndicator", "QAIndicator"),
        ("QUANTAXIS.QAMarket", "QAMarket"),
        ("QUANTAXIS.QASU", "QASU"),
        ("QUANTAXIS.QIFI", "QIFI"),
    ):
        _namespace(name, root / relative)

    parameters = _load_source(
        "QUANTAXIS.QAUtil.QAParameter", root / "QAUtil" / "QAParameter.py"
    )
    util = sys.modules["QUANTAXIS.QAUtil"]
    util.DATABASE = None
    util.QA_util_log_info = lambda *args, **kwargs: None

    data_fq = _load_source("QUANTAXIS.QAData.data_fq", root / "QAData" / "data_fq.py")
    indicator_base = _load_source(
        "QUANTAXIS.QAIndicator.base", root / "QAIndicator" / "base.py"
    )
    indicators = _load_source(
        "QUANTAXIS.QAIndicator.indicators", root / "QAIndicator" / "indicators.py"
    )
    calendar = _load_source(
        "QUANTAXIS.QAUtil.QADate_trade", root / "QAUtil" / "QADate_trade.py"
    )
    market_preset = _load_source(
        "QUANTAXIS.QAMarket.market_preset", root / "QAMarket" / "market_preset.py"
    )

    class _QAOrderPlaceholder:
        pass

    _stub(
        "QUANTAXIS.QAMarket.QAOrder",
        ORDER_DIRECTION=parameters.ORDER_DIRECTION,
        QA_Order=_QAOrderPlaceholder,
    )
    _stub("QUANTAXIS.QASU.save_position", save_position=lambda value: None)
    _stub("QUANTAXIS.QAUtil.QASetting", DATABASE=None)
    position = _load_source(
        "QUANTAXIS.QAMarket.QAPosition", root / "QAMarket" / "QAPosition.py"
    )
    qifi = _load_source("QUANTAXIS.QIFI.QifiAccount", root / "QIFI" / "QifiAccount.py")

    return {
        "version": installed_version,
        "data_fq": data_fq,
        "indicators": indicators,
        "indicator_base": indicator_base,
        "calendar": calendar,
        "market_preset": market_preset,
        "position": position,
        "qifi": qifi,
        "parameters": parameters,
        "source_sha256": source_sha256,
    }



def capabilities(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    _, installed_version, source_sha256 = _verify_installation()
    return {
        "engine_version": installed_version,
        "engine_commit": ENGINE_COMMIT,
        "source_sha256": source_sha256,
        "operations": {
            "capabilities": "poc",
            "security_probe": "poc",
            "direct_smoke": "poc",
            "adjust_prices": "qe2",
            "trading_calendar": "qe2",
            "compute_factors": {
                "status": "qe2",
                "whitelist": ["ma", "ema"],
            },
            "backtest": "qe5",
            "normalize_ledger": "not_available_until_qe5_3",
        },
        "protocol": {"name": "vibe.quant-engine.jsonl", "schema_version": "1.0"},
    }


def direct_smoke(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    try:
        import pandas as pd
        boundary = _load_quantaxis_boundary()
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("ENGINE_IMPORT_ERROR", f"{type(exc).__name__}: {exc}") from exc

    dates = pd.to_datetime(["2024-06-12", "2024-06-13", "2024-06-14", "2024-06-17"])
    raw = pd.DataFrame(
        {
            "open": [10.0, 10.2, 9.6, 9.8],
            "high": [10.3, 10.4, 9.9, 10.1],
            "low": [9.9, 10.0, 9.5, 9.7],
            "close": [10.1, 10.2, 9.8, 10.0],
            "volume": [1000, 1200, 1100, 1300],
        },
        index=dates,
    )
    xdxr = pd.DataFrame(
        {
            "category": [1],
            "fenhong": [4.0],
            "peigu": [0.0],
            "peigujia": [0.0],
            "songzhuangu": [0.0],
        },
        index=pd.to_datetime(["2024-06-14"]),
    )
    qfq = boundary["data_fq"]._QA_data_stock_to_fq(raw.copy(), xdxr.copy(), "qfq")
    hfq = boundary["data_fq"]._QA_data_stock_to_fq(raw.copy(), xdxr.copy(), "hfq")
    ma = boundary["indicators"].QA_indicator_MA(raw, 2)
    calendar = [
        day
        for day in boundary["calendar"].trade_date_sse
        if "2024-06-12" <= day <= "2024-06-17"
    ]

    account = boundary["qifi"].QIFI_Account(
        "qe0-poc",
        "not-a-secret",
        model="BACKTEST",
        init_cash=100_000,
        nodatabase=True,
    )
    account.create_backtestaccount()
    order = account.send_order(
        "000001",
        100,
        10.0,
        boundary["parameters"].ORDER_DIRECTION.BUY,
        order_id="qe0-order-1",
        datetime="2024-06-12 09:31:00",
    )
    if not order:
        raise WorkerError("ENGINE_SEMANTIC_ERROR", "offline QIFI account rejected minimal buy")
    account.make_deal(order)
    completed = account.orders["qe0-order-1"]
    if completed["status"] != "FINISHED" or completed["volume_left"] != 0:
        raise WorkerError("ENGINE_SEMANTIC_ERROR", "QIFI minimal fill did not complete")

    return {
        "import": {
            "version": boundary["version"],
            "boundary": "pinned_source_modules",
            "top_level_package_executed": False,
            "source_sha256": boundary["source_sha256"],
        },
        "adjust_prices": {
            "qfq_close": [round(float(value), 8) for value in qfq["close"]],
            "hfq_close": [round(float(value), 8) for value in hfq["close"]],
        },
        "trading_calendar": calendar,
        "factor": {"ma2": [None if pd.isna(value) else round(float(value), 8) for value in ma["MA2"]]},
        "account_backtest": {
            "order_status": completed["status"],
            "volume_left": completed["volume_left"],
            "trade_count": len(account.trades),
            "position_count": len(account.positions),
        },
    }


if __name__ == "__main__":
    raise SystemExit(
        run_worker(
            engine_name=ENGINE_NAME,
            engine_commit=ENGINE_COMMIT,
            handlers={
                "capabilities": capabilities,
                "direct_smoke": direct_smoke,
                **build_formal_handlers(_load_quantaxis_base_boundary),
                "backtest": build_backtest_handler(
                    _load_quantaxis_backtest_boundary
                ),
            },
        )
    )
