"""Deterministic QE4 strategy templates over the strict QE1 contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    model_validator,
)

from src.research.contracts import (
    CostSpec,
    EvaluationSpec,
    ExecutionSpec,
    PortfolioSpec,
    RankingSpec,
    ResearchObject,
    ResearchSpec,
    RiskSpec,
    SignalRule,
    SimilarityRun,
    StrategySpec,
    create_research_object,
)

from .capabilities import resolve_strategy_field
from .validation import require_valid_strategy_spec

STRATEGY_TEMPLATE_VERSION = "vibe.strategy-template.v1"

StrategyTemplateId = Literal[
    "top_n_rebalance",
    "factor_threshold",
    "factor_trend_confirmation",
]
StrategySourceMode = Literal["direct", "similarity_run"]
ThresholdOperator = Literal["gt", "gte", "lt", "lte"]
RebalanceFrequency = Literal["daily", "weekly", "monthly"]


class StrategyTemplateError(ValueError):
    """Raised when deterministic template inputs are inconsistent."""


class _TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyTemplateSource(_TemplateModel):
    """One explicit direct or SimilarityRun strategy source."""

    research: ResearchObject
    snapshot: ResearchObject
    similarity_run: ResearchObject | None = None
    universe_symbols: tuple[str, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_source_chain(self) -> "StrategyTemplateSource":
        if self.research.object_type != "research_spec" or not isinstance(
            self.research.payload, ResearchSpec
        ):
            raise ValueError("research must be a research_spec ResearchObject")
        if self.snapshot.object_type != "data_snapshot_ref":
            raise ValueError("snapshot must be a data_snapshot_ref ResearchObject")
        if self.research.owner_scope != self.snapshot.owner_scope:
            raise ValueError("research and snapshot must share owner_scope")
        if self.research.ref() not in self.snapshot.parent_refs:
            raise ValueError("snapshot must descend directly from research")

        if self.similarity_run is None:
            if not self.universe_symbols:
                raise ValueError("direct source requires universe_symbols")
            return self

        if self.universe_symbols:
            raise ValueError("similarity source derives its universe from candidates")
        if self.similarity_run.object_type != "similarity_run" or not isinstance(
            self.similarity_run.payload, SimilarityRun
        ):
            raise ValueError("similarity_run must be a similarity_run ResearchObject")
        if self.similarity_run.owner_scope != self.research.owner_scope:
            raise ValueError("similarity source must share owner_scope")
        payload = self.similarity_run.payload
        if payload.research_spec_ref != self.research.ref():
            raise ValueError("similarity source references a different research_spec")
        if payload.data_snapshot_ref != self.snapshot.ref():
            raise ValueError("similarity source references a different data snapshot")
        required_parents = {self.research.ref(), self.snapshot.ref()}
        if not required_parents.issubset(set(self.similarity_run.parent_refs)):
            raise ValueError("similarity source omits research or snapshot ancestry")
        return self

    @property
    def source_mode(self) -> StrategySourceMode:
        return "similarity_run" if self.similarity_run is not None else "direct"

    @property
    def resolved_universe(self) -> tuple[str, ...]:
        if self.similarity_run is None:
            return self.universe_symbols
        payload = self.similarity_run.payload
        assert isinstance(payload, SimilarityRun)
        return tuple(candidate.symbol for candidate in payload.candidates)


class _CommonTemplate(_TemplateModel):
    title: str = Field(min_length=1, max_length=200)
    rebalance: RebalanceFrequency
    max_positions: int = Field(ge=1, le=500)
    max_position_weight: float = Field(gt=0.0, le=1.0)
    cash_buffer_weight: float = Field(default=0.0, ge=0.0, lt=1.0)
    costs: CostSpec
    risk: RiskSpec
    evaluation: EvaluationSpec


class TopNRebalanceTemplate(_CommonTemplate):
    """Select the top N securities by one allowlisted ranking field."""

    template_id: Literal["top_n_rebalance"] = "top_n_rebalance"
    ranking_field: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    ranking_direction: Literal["ascending", "descending"]
    top_n: int = Field(ge=1, le=500)


class FactorThresholdTemplate(_CommonTemplate):
    """Hold securities satisfying one allowlisted factor threshold."""

    template_id: Literal["factor_threshold"] = "factor_threshold"
    factor_field: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    operator: ThresholdOperator
    threshold: StrictFloat | StrictInt


class FactorTrendConfirmationTemplate(_CommonTemplate):
    """Require a factor threshold and a close/trend crossing confirmation."""

    template_id: Literal["factor_trend_confirmation"] = "factor_trend_confirmation"
    factor_field: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    operator: ThresholdOperator
    threshold: StrictFloat | StrictInt
    trend_kind: Literal["ma", "ema"]
    trend_window: int = Field(ge=2, le=512)


StrategyTemplate = (
    TopNRebalanceTemplate
    | FactorThresholdTemplate
    | FactorTrendConfirmationTemplate
)


class StrategyTemplateBuild(_TemplateModel):
    """Auditable result shared by all three deterministic template paths."""

    schema_version: Literal["vibe.strategy-template.v1"] = STRATEGY_TEMPLATE_VERSION
    template_id: StrategyTemplateId
    source_mode: StrategySourceMode
    strategy_object: ResearchObject

    @model_validator(mode="after")
    def validate_build(self) -> "StrategyTemplateBuild":
        if self.strategy_object.object_type != "strategy_spec" or not isinstance(
            self.strategy_object.payload, StrategySpec
        ):
            raise ValueError("strategy_object must contain StrategySpec")
        spec = self.strategy_object.payload
        if self.source_mode == "direct" and spec.similarity_run_ref is not None:
            raise ValueError("direct build cannot reference SimilarityRun")
        if self.source_mode == "similarity_run" and spec.similarity_run_ref is None:
            raise ValueError("similarity build must reference SimilarityRun")
        _validate_template_shape(self.template_id, spec)
        return self


def _field_lookback(field_id: str) -> int:
    capability = resolve_strategy_field(field_id)
    if capability is None:
        raise StrategyTemplateError(f"field {field_id!r} is not allowlisted")
    return capability.lookback_days


def _factor_lookback(field_id: str) -> int:
    capability = resolve_strategy_field(field_id)
    if capability is None:
        raise StrategyTemplateError(f"field {field_id!r} is not allowlisted")
    if capability.kind != "factor":
        raise StrategyTemplateError(f"field {field_id!r} is not a factor")
    return capability.lookback_days


def _portfolio(template: _CommonTemplate) -> PortfolioSpec:
    return PortfolioSpec(
        max_positions=template.max_positions,
        max_position_weight=template.max_position_weight,
        cash_buffer_weight=template.cash_buffer_weight,
    )


def _execution(template: _CommonTemplate) -> ExecutionSpec:
    return ExecutionSpec(rebalance=template.rebalance)


def _validate_template_shape(
    template_id: StrategyTemplateId,
    spec: StrategySpec,
) -> None:
    if template_id == "top_n_rebalance":
        valid = (
            len(spec.signals) == 1
            and spec.signals[0]
            == SignalRule(
                field="close",
                operator="gt",
                value=0.0,
                lookback_days=1,
            )
            and spec.ranking is not None
        )
    elif template_id == "factor_threshold":
        factor = (
            resolve_strategy_field(spec.signals[0].field)
            if len(spec.signals) == 1
            else None
        )
        valid = (
            len(spec.signals) == 1
            and factor is not None
            and factor.kind == "factor"
            and spec.signals[0].operator in {"gt", "gte", "lt", "lte"}
            and spec.ranking is None
        )
    else:
        trend = spec.signals[1] if len(spec.signals) == 2 else None
        factor = (
            resolve_strategy_field(spec.signals[0].field)
            if len(spec.signals) == 2
            else None
        )
        valid = (
            trend is not None
            and factor is not None
            and factor.kind == "factor"
            and spec.signals[0].operator in {"gt", "gte", "lt", "lte"}
            and trend.field == "close"
            and trend.operator == "crosses_above"
            and isinstance(trend.value, str)
            and trend.value.startswith(("ma_", "ema_"))
            and spec.ranking is None
        )
    if not valid:
        raise ValueError(f"strategy does not match {template_id} fixed shape")


def build_strategy_template(
    template: StrategyTemplate,
    *,
    source: StrategyTemplateSource,
    created_at: datetime | None = None,
) -> StrategyTemplateBuild:
    """Build one strict StrategySpec object without LLM or worker execution."""

    universe = source.resolved_universe
    common = {
        "research_spec_ref": source.research.ref(),
        "similarity_run_ref": (
            source.similarity_run.ref() if source.similarity_run is not None else None
        ),
        "data_snapshot_ref": source.snapshot.ref(),
        "title": template.title,
        "universe_symbols": universe,
        "portfolio": _portfolio(template),
        "execution": _execution(template),
        "costs": template.costs,
        "risk": template.risk,
        "evaluation": template.evaluation,
    }

    if isinstance(template, TopNRebalanceTemplate):
        _field_lookback(template.ranking_field)
        spec = StrategySpec(
            **common,
            signals=(
                SignalRule(
                    field="close",
                    operator="gt",
                    value=0.0,
                    lookback_days=1,
                ),
            ),
            ranking=RankingSpec(
                field=template.ranking_field,
                direction=template.ranking_direction,
                top_n=template.top_n,
            ),
        )
    elif isinstance(template, FactorThresholdTemplate):
        spec = StrategySpec(
            **common,
            signals=(
                SignalRule(
                    field=template.factor_field,
                    operator=template.operator,
                    value=template.threshold,
                    lookback_days=_factor_lookback(template.factor_field),
                ),
            ),
        )
    elif isinstance(template, FactorTrendConfirmationTemplate):
        trend_field = f"{template.trend_kind}_{template.trend_window}"
        spec = StrategySpec(
            **common,
            signals=(
                SignalRule(
                    field=template.factor_field,
                    operator=template.operator,
                    value=template.threshold,
                    lookback_days=_factor_lookback(template.factor_field),
                ),
                SignalRule(
                    field="close",
                    operator="crosses_above",
                    value=trend_field,
                    lookback_days=1,
                ),
            ),
        )
    else:
        raise TypeError("unsupported strategy template model")

    require_valid_strategy_spec(spec, snapshot=source.snapshot)
    parent_refs = [source.research.ref(), source.snapshot.ref()]
    if source.similarity_run is not None:
        parent_refs.append(source.similarity_run.ref())
    strategy_object = create_research_object(
        spec,
        owner_scope=source.research.owner_scope,
        parent_refs=parent_refs,
        created_at=created_at,
    )
    return StrategyTemplateBuild(
        template_id=template.template_id,
        source_mode=source.source_mode,
        strategy_object=strategy_object,
    )
