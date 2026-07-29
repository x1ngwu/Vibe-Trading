"""Fail-closed semantic validation for QE4 StrategySpec drafts."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.research.contracts import DataSnapshotRef, ResearchObject, StrategySpec

from .capabilities import allowed_operator, resolve_strategy_field

StrategyIssueCode = Literal[
    "allocation_infeasible",
    "a_share_rule_mismatch",
    "crossing_consecutive_days",
    "duplicate_signal",
    "evaluation_after_snapshot",
    "field_self_reference",
    "invalid_field_reference",
    "invalid_value",
    "lookback_mismatch",
    "positions_exceed_ranking",
    "positions_exceed_universe",
    "ranking_exceeds_universe",
    "snapshot_ref_mismatch",
    "symbol_outside_snapshot",
    "unknown_field",
    "unsupported_operator",
]


class StrategyValidationIssue(BaseModel):
    """One stable machine-readable semantic failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: StrategyIssueCode
    path: str = Field(pattern=r"^[a-z][a-z0-9_.\[\]-]{0,127}$")
    message: str = Field(min_length=1, max_length=500)


class StrategyValidationResult(BaseModel):
    """Deterministic validation result suitable for API and confirmation UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    issues: tuple[StrategyValidationIssue, ...] = ()


class StrategySemanticError(ValueError):
    """Raised when a strategy cannot enter confirmation."""

    def __init__(self, result: StrategyValidationResult):
        self.result = result
        summary = "; ".join(f"{item.path}: {item.code}" for item in result.issues)
        super().__init__(summary or "strategy semantic validation failed")


def _issue(
    issues: list[StrategyValidationIssue],
    code: StrategyIssueCode,
    path: str,
    message: str,
) -> None:
    issues.append(StrategyValidationIssue(code=code, path=path, message=message))


def _snapshot_payload(snapshot: ResearchObject) -> DataSnapshotRef:
    if snapshot.object_type != "data_snapshot_ref" or not isinstance(
        snapshot.payload, DataSnapshotRef
    ):
        raise TypeError("snapshot must be a data_snapshot_ref ResearchObject")
    return snapshot.payload


def validate_strategy_spec(
    spec: StrategySpec,
    *,
    snapshot: ResearchObject | None = None,
) -> StrategyValidationResult:
    """Validate one strict draft without invoking an LLM, worker, or provider."""

    issues: list[StrategyValidationIssue] = []
    signal_identities: set[tuple[object, ...]] = set()

    for index, rule in enumerate(spec.signals):
        base_path = f"signals[{index}]"
        capability = resolve_strategy_field(rule.field)
        if capability is None:
            _issue(
                issues,
                "unknown_field",
                f"{base_path}.field",
                f"field {rule.field!r} is not in {spec.object_type} DSL allowlist",
            )
            continue
        if not allowed_operator(rule, capability):
            _issue(
                issues,
                "unsupported_operator",
                f"{base_path}.operator",
                f"operator {rule.operator!r} is not allowed for {rule.field!r}",
            )
        if rule.lookback_days != capability.lookback_days:
            _issue(
                issues,
                "lookback_mismatch",
                f"{base_path}.lookback_days",
                f"{rule.field!r} requires lookback_days={capability.lookback_days}",
            )

        if rule.operator in {"crosses_above", "crosses_below"}:
            if rule.consecutive_days != 1:
                _issue(
                    issues,
                    "crossing_consecutive_days",
                    f"{base_path}.consecutive_days",
                    "crossing rules must use consecutive_days=1",
                )
            if not isinstance(rule.value, str):
                _issue(
                    issues,
                    "invalid_field_reference",
                    f"{base_path}.value",
                    "crossing rules must reference another allowlisted field",
                )
            elif rule.value == rule.field:
                _issue(
                    issues,
                    "field_self_reference",
                    f"{base_path}.value",
                    "a field cannot cross itself",
                )
            elif resolve_strategy_field(rule.value) is None:
                _issue(
                    issues,
                    "invalid_field_reference",
                    f"{base_path}.value",
                    f"referenced field {rule.value!r} is not allowlisted",
                )
        elif (
            isinstance(rule.value, bool)
            or not isinstance(rule.value, (int, float))
            or not math.isfinite(float(rule.value))
        ):
            _issue(
                issues,
                "invalid_value",
                f"{base_path}.value",
                "threshold comparisons require one finite numeric value",
            )

        identity = (
            rule.field,
            rule.operator,
            rule.value,
            rule.lookback_days,
            rule.consecutive_days,
        )
        if identity in signal_identities:
            _issue(
                issues,
                "duplicate_signal",
                base_path,
                "signals must not contain duplicate semantic rules",
            )
        signal_identities.add(identity)

    if spec.ranking is not None:
        ranking_capability = resolve_strategy_field(spec.ranking.field)
        if ranking_capability is None:
            _issue(
                issues,
                "unknown_field",
                "ranking.field",
                f"ranking field {spec.ranking.field!r} is not allowlisted",
            )
        if spec.ranking.top_n > len(spec.universe_symbols):
            _issue(
                issues,
                "ranking_exceeds_universe",
                "ranking.top_n",
                "top_n cannot exceed the fixed strategy universe",
            )
        if spec.portfolio.max_positions > spec.ranking.top_n:
            _issue(
                issues,
                "positions_exceed_ranking",
                "portfolio.max_positions",
                "max_positions cannot exceed ranking top_n",
            )

    if spec.portfolio.max_positions > len(spec.universe_symbols):
        _issue(
            issues,
            "positions_exceed_universe",
            "portfolio.max_positions",
            "max_positions cannot exceed the fixed strategy universe",
        )

    investable_weight = 1.0 - spec.portfolio.cash_buffer_weight
    maximum_allocatable = (
        spec.portfolio.max_positions * spec.portfolio.max_position_weight
    )
    if maximum_allocatable + 1e-12 < investable_weight:
        _issue(
            issues,
            "allocation_infeasible",
            "portfolio.max_position_weight",
            "position caps cannot allocate the non-cash portfolio weight",
        )

    if not spec.execution.enforce_t_plus_one:
        _issue(
            issues,
            "a_share_rule_mismatch",
            "execution.enforce_t_plus_one",
            "the first A-share DSL requires T+1 enforcement",
        )
    if spec.execution.board_lot != 100:
        _issue(
            issues,
            "a_share_rule_mismatch",
            "execution.board_lot",
            "the first A-share DSL requires a 100-share board lot",
        )

    if snapshot is not None:
        snapshot_payload = _snapshot_payload(snapshot)
        if spec.data_snapshot_ref != snapshot.ref():
            _issue(
                issues,
                "snapshot_ref_mismatch",
                "data_snapshot_ref",
                "strategy and supplied snapshot identities differ",
            )
        for index, symbol in enumerate(spec.universe_symbols):
            if symbol not in snapshot_payload.symbols:
                _issue(
                    issues,
                    "symbol_outside_snapshot",
                    f"universe_symbols[{index}]",
                    f"symbol {symbol!r} is absent from the fixed snapshot",
                )
        if spec.evaluation.test_end > snapshot_payload.as_of:
            _issue(
                issues,
                "evaluation_after_snapshot",
                "evaluation.test_end",
                "evaluation cannot use data after the snapshot as_of",
            )

    ordered = tuple(sorted(issues, key=lambda item: (item.path, item.code, item.message)))
    return StrategyValidationResult(valid=not ordered, issues=ordered)


def require_valid_strategy_spec(
    spec: StrategySpec,
    *,
    snapshot: ResearchObject,
) -> StrategySpec:
    """Return the same immutable spec or raise before confirmation/compilation."""

    result = validate_strategy_spec(spec, snapshot=snapshot)
    if not result.valid:
        raise StrategySemanticError(result)
    return spec
