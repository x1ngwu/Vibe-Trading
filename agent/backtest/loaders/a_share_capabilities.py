"""QE2 capability decisions for the existing A-share fallback chain.

Only sources whose current loader preserves a provable adjustment, complete
field set, and field units are exposed to the strict data envelope.  A source
remaining in the legacy registry is not evidence that it is semantically safe
for research or backtests.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.loaders.akshare_loader import QE2_A_SHARE_DAILY_RAW_CAPABILITY
from backtest.loaders.data_envelope import LoaderCapability
from backtest.loaders.registry import FALLBACK_CHAINS
from backtest.loaders.tushare import QE2_DAILY_CAPABILITY


@dataclass(frozen=True)
class AShareCapabilityDecision:
    source: str
    capability: LoaderCapability | None
    blocked_reason: str | None

    def __post_init__(self) -> None:
        if (self.capability is None) == (self.blocked_reason is None):
            raise ValueError("exactly one of capability or blocked_reason is required")
        if self.capability is not None and self.capability.source != self.source:
            raise ValueError("capability source must match decision source")

    @property
    def enabled(self) -> bool:
        return self.capability is not None

    @property
    def decision(self) -> str:
        """Final QE2 strict-path decision; legacy registry availability is separate."""

        return "adopt" if self.enabled else "drop"


A_SHARE_QE2_CAPABILITY_DECISIONS: tuple[AShareCapabilityDecision, ...] = (
    AShareCapabilityDecision(
        source="tencent",
        capability=None,
        blocked_reason="loader returns qfq data but omits amount; adjustment is not an input",
    ),
    AShareCapabilityDecision(
        source="mootdx",
        capability=None,
        blocked_reason="loader omits amount and does not declare a verifiable adjustment contract",
    ),
    AShareCapabilityDecision(
        source="eastmoney",
        capability=None,
        blocked_reason="loader hard-codes fqt=1 qfq and omits amount",
    ),
    AShareCapabilityDecision(
        source="baostock",
        capability=None,
        blocked_reason="loader hard-codes qfq and discards the fetched amount column",
    ),
    AShareCapabilityDecision(
        source="akshare",
        capability=QE2_A_SHARE_DAILY_RAW_CAPABILITY,
        blocked_reason=None,
    ),
    AShareCapabilityDecision(
        source="tushare",
        capability=QE2_DAILY_CAPABILITY,
        blocked_reason=None,
    ),
    AShareCapabilityDecision(
        source="local",
        capability=None,
        blocked_reason="user-defined local schemas do not yet carry adjustment and unit declarations",
    ),
)


AKSHARE_QE2_INSTRUMENT_DECISIONS: dict[str, tuple[str, str | None]] = {
    "stock": ("adopt", None),
    "etf": (
        "drop",
        "AKShare ETF path has no reviewed strict raw OHLCVA adapter/unit contract",
    ),
    "index": (
        "drop",
        "AKShare index path has no reviewed strict raw OHLCVA adapter/unit contract",
    ),
}


def qe2_a_share_drop_decisions() -> dict[str, str]:
    """Return final source drops from the QE2 strict path with review rationale."""

    _validate_decision_coverage()
    return {
        item.source: str(item.blocked_reason)
        for item in A_SHARE_QE2_CAPABILITY_DECISIONS
        if item.decision == "drop"
    }


def qe2_a_share_capabilities() -> dict[str, LoaderCapability]:
    """Return only capabilities safe for the strict QE2 envelope."""

    _validate_decision_coverage()
    return {
        item.source: item.capability
        for item in A_SHARE_QE2_CAPABILITY_DECISIONS
        if item.capability is not None
    }


def _validate_decision_coverage() -> None:
    expected = tuple(FALLBACK_CHAINS["a_share"])
    actual = tuple(item.source for item in A_SHARE_QE2_CAPABILITY_DECISIONS)
    if actual != expected:
        raise ValueError(
            f"A-share QE2 capability decisions drifted from fallback chain: "
            f"expected={expected}, actual={actual}"
        )
