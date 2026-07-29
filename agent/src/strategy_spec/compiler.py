"""Deterministic QE4 StrategySpec-to-EngineRequest compiler.

This module only creates strict data.  It deliberately has no worker, subprocess,
network, generated-code, or engine-adapter dependency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import (
    EngineIdentitySpec,
    EngineRequest,
    ObjectRef,
    ResearchObject,
    ResourceLimits,
    StrategySpec,
    canonical_sha256,
    create_research_object,
)

from .capabilities import (
    STRATEGY_DSL_VERSION,
    StrategyFieldKind,
    StrategyFieldSource,
    resolve_strategy_field,
)
from .templates import (
    STRATEGY_TEMPLATE_VERSION,
    StrategySourceMode,
    StrategyTemplateBuild,
    StrategyTemplateId,
)
from .validation import require_valid_strategy_spec

STRATEGY_PLAN_VERSION = "vibe.strategy-execution-plan.v1"


class _CompilerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompiledFieldBinding(_CompilerModel):
    """Exact implementation path for one field required by a strategy."""

    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: StrategyFieldKind
    source: StrategyFieldSource
    lookback_days: int = Field(ge=1, le=512)
    available_at: Literal["close"] = "close"


class CompiledStrategyPlan(_CompilerModel):
    """Content-identified, engine-neutral execution plan."""

    schema_version: Literal[
        "vibe.strategy-execution-plan.v1"
    ] = STRATEGY_PLAN_VERSION
    dsl_version: Literal["vibe.strategy-spec.v1"] = STRATEGY_DSL_VERSION
    template_version: Literal[
        "vibe.strategy-template.v1"
    ] = STRATEGY_TEMPLATE_VERSION
    template_id: StrategyTemplateId
    source_mode: StrategySourceMode
    plan_id: str = Field(pattern=r"^strategy-plan:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    strategy: StrategySpec
    engine: EngineIdentitySpec
    operation: Literal["backtest"] = "backtest"
    field_bindings: tuple[CompiledFieldBinding, ...] = Field(min_length=1)
    resource_limits: ResourceLimits
    random_seed: int = Field(ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_identity(self) -> "CompiledStrategyPlan":
        material = self.model_dump(
            mode="json",
            exclude={"plan_id", "content_sha256"},
        )
        expected = canonical_sha256(material)
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match compiled plan")
        if self.plan_id != f"strategy-plan:{expected}":
            raise ValueError("plan_id does not match compiled plan")
        if self.strategy_spec_ref.object_type != "strategy_spec":
            raise ValueError("strategy_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        if self.strategy.data_snapshot_ref != self.data_snapshot_ref:
            raise ValueError("embedded strategy and plan snapshot refs differ")
        expected_bindings = _field_bindings(self.strategy)
        if self.field_bindings != expected_bindings:
            raise ValueError("field_bindings do not match the embedded strategy")
        return self


class StrategyCompilation(_CompilerModel):
    """Fixed plan plus its idempotent EngineRequest research object."""

    plan: CompiledStrategyPlan
    engine_request: ResearchObject

    @model_validator(mode="after")
    def validate_request(self) -> "StrategyCompilation":
        if self.engine_request.object_type != "engine_request" or not isinstance(
            self.engine_request.payload, EngineRequest
        ):
            raise ValueError("engine_request must contain EngineRequest")
        request = self.engine_request.payload
        if request.strategy_spec_ref != self.plan.strategy_spec_ref:
            raise ValueError("plan and EngineRequest strategy refs differ")
        if request.data_snapshot_ref != self.plan.data_snapshot_ref:
            raise ValueError("plan and EngineRequest snapshot refs differ")
        if request.engine != self.plan.engine:
            raise ValueError("plan and EngineRequest engine identities differ")
        if request.resource_limits != self.plan.resource_limits:
            raise ValueError("plan and EngineRequest resource limits differ")
        if request.random_seed != self.plan.random_seed:
            raise ValueError("plan and EngineRequest random seeds differ")
        expected_request_id = (
            f"qe4:{self.plan.template_id}:{self.plan.content_sha256[:24]}"
        )
        if request.request_id != expected_request_id:
            raise ValueError("EngineRequest request_id does not match plan identity")
        if request.operation != self.plan.operation:
            raise ValueError("plan and EngineRequest operations differ")
        expected_parents = {
            self.plan.strategy_spec_ref,
            self.plan.data_snapshot_ref,
        }
        if set(self.engine_request.parent_refs) != expected_parents:
            raise ValueError("EngineRequest ancestry does not match compiled inputs")
        return self


def _field_bindings(spec: StrategySpec) -> tuple[CompiledFieldBinding, ...]:
    field_ids = {rule.field for rule in spec.signals}
    field_ids.update(
        rule.value
        for rule in spec.signals
        if rule.operator in {"crosses_above", "crosses_below"}
        and isinstance(rule.value, str)
    )
    if spec.ranking is not None:
        field_ids.add(spec.ranking.field)

    bindings: list[CompiledFieldBinding] = []
    for field_id in sorted(field_ids):
        capability = resolve_strategy_field(field_id)
        if capability is None:
            raise ValueError(f"compiled field {field_id!r} is not allowlisted")
        bindings.append(
            CompiledFieldBinding(
                field_id=capability.field_id,
                kind=capability.kind,
                source=capability.source,
                lookback_days=capability.lookback_days,
                available_at=capability.available_at,
            )
        )
    return tuple(bindings)


def compile_strategy_template(
    build: StrategyTemplateBuild,
    *,
    snapshot: ResearchObject,
    engine: EngineIdentitySpec,
    resource_limits: ResourceLimits,
    random_seed: int,
    created_at: datetime | None = None,
) -> StrategyCompilation:
    """Compile a validated template to immutable plan and EngineRequest data."""

    strategy_object = build.strategy_object
    if strategy_object.owner_scope != snapshot.owner_scope:
        raise ValueError("strategy and snapshot must share owner_scope")
    if not isinstance(strategy_object.payload, StrategySpec):
        raise TypeError("strategy_object must contain StrategySpec")
    spec = require_valid_strategy_spec(strategy_object.payload, snapshot=snapshot)
    bindings = _field_bindings(spec)
    material = {
        "schema_version": STRATEGY_PLAN_VERSION,
        "dsl_version": STRATEGY_DSL_VERSION,
        "template_version": STRATEGY_TEMPLATE_VERSION,
        "template_id": build.template_id,
        "source_mode": build.source_mode,
        "strategy_spec_ref": strategy_object.ref().model_dump(mode="json"),
        "data_snapshot_ref": snapshot.ref().model_dump(mode="json"),
        "strategy": spec.model_dump(mode="json"),
        "engine": engine.model_dump(mode="json"),
        "operation": "backtest",
        "field_bindings": [
            binding.model_dump(mode="json") for binding in bindings
        ],
        "resource_limits": resource_limits.model_dump(mode="json"),
        "random_seed": random_seed,
    }
    digest = canonical_sha256(material)
    plan = CompiledStrategyPlan(
        **material,
        plan_id=f"strategy-plan:{digest}",
        content_sha256=digest,
    )
    request = EngineRequest(
        request_id=f"qe4:{build.template_id}:{digest[:24]}",
        strategy_spec_ref=strategy_object.ref(),
        data_snapshot_ref=snapshot.ref(),
        engine=engine,
        operation="backtest",
        resource_limits=resource_limits,
        random_seed=random_seed,
    )
    request_object = create_research_object(
        request,
        owner_scope=strategy_object.owner_scope,
        parent_refs=(strategy_object.ref(), snapshot.ref()),
        created_at=created_at,
    )
    return StrategyCompilation(plan=plan, engine_request=request_object)
