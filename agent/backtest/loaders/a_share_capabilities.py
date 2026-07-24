"""QE2 capability decisions for the existing A-share fallback chain.

Only sources whose current loader preserves a provable adjustment, complete
field set, and field units are exposed to the strict data envelope.  A source
remaining in the legacy registry is not evidence that it is semantically safe
for research or backtests.
"""

from __future__ import annotations

from dataclasses import dataclass

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
        capability=None,
        blocked_reason="stock path hard-codes qfq, ETF semantics differ, and amount is omitted",
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
