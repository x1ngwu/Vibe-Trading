"""QE4 deterministic StrategySpec DSL and validation boundary."""

from .capabilities import (
    STRATEGY_DSL_VERSION,
    SignalOperator,
    StrategyFieldCapability,
    StrategyFieldKind,
    StrategyFieldSource,
    allowed_operator,
    list_strategy_field_capabilities,
    resolve_strategy_field,
)
from .validation import (
    StrategyIssueCode,
    StrategySemanticError,
    StrategyValidationIssue,
    StrategyValidationResult,
    require_valid_strategy_spec,
    validate_strategy_spec,
)

__all__ = [
    "STRATEGY_DSL_VERSION",
    "SignalOperator",
    "StrategyFieldCapability",
    "StrategyFieldKind",
    "StrategyFieldSource",
    "StrategyIssueCode",
    "StrategySemanticError",
    "StrategyValidationIssue",
    "StrategyValidationResult",
    "allowed_operator",
    "list_strategy_field_capabilities",
    "require_valid_strategy_spec",
    "resolve_strategy_field",
    "validate_strategy_spec",
]
