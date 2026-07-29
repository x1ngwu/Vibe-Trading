"""QE4-2 deterministic strategy templates and compiler gates."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.research.contracts import (
    ChannelWeights,
    CostSpec,
    DataSnapshotRef,
    EngineIdentitySpec,
    EvaluationSpec,
    ResearchSpec,
    ResourceLimits,
    RiskSpec,
    SimilarityRun,
    StockCandidate,
    create_research_object,
)
from src.strategy_spec import (
    STRATEGY_DSL_VERSION,
    STRATEGY_PLAN_VERSION,
    STRATEGY_TEMPLATE_VERSION,
    FactorThresholdTemplate,
    FactorTrendConfirmationTemplate,
    StrategySemanticError,
    StrategyTemplateBuild,
    StrategyTemplateError,
    StrategyTemplateSource,
    TopNRebalanceTemplate,
    build_strategy_template,
    compile_strategy_template,
)

_QA_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"
_CREATED_AT = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


def _chain():
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH",),
            as_of=date(2025, 6, 30),
            lookback_days=(20, 60),
            candidate_universe="qe4-fixture",
            requested_outputs=("similarity", "strategy", "backtest"),
        ),
        created_at=_CREATED_AT,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="1" * 64,
            as_of=date(2025, 6, 30),
            start_date=date(2023, 1, 3),
            end_date=date(2025, 6, 30),
            adjustment="qfq",
            symbols=("600519.SH", "000858.SZ", "000001.SZ"),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={
                "600519.SH": "fixture",
                "000858.SZ": "fixture",
                "000001.SZ": "fixture",
            },
        ),
        parent_refs=(research.ref(),),
        created_at=_CREATED_AT,
    )
    similarity = create_research_object(
        SimilarityRun(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            factor_evidence_refs=(),
            weights=ChannelWeights(business=0.3, factor=0.4, price_volume=0.3),
            candidates=(
                StockCandidate(
                    symbol="000858.SZ",
                    rank=1,
                    business_score=0.8,
                    factor_score=0.9,
                    price_volume_score=0.7,
                    combined_score=0.81,
                    coverage=1.0,
                    evidence=("same-industry",),
                    counterevidence=("higher-volatility",),
                ),
                StockCandidate(
                    symbol="000001.SZ",
                    rank=2,
                    business_score=0.7,
                    factor_score=0.8,
                    price_volume_score=0.6,
                    combined_score=0.71,
                    coverage=1.0,
                    evidence=("similar-momentum",),
                    counterevidence=("different-industry",),
                ),
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=_CREATED_AT,
    )
    return research, snapshot, similarity


def _costs() -> CostSpec:
    return CostSpec(
        commission_bps=3.0,
        minimum_commission=5.0,
        sell_tax_bps=5.0,
        transfer_fee_bps=0.1,
        slippage_bps=5.0,
        rule_version="cn-equity-2025-01-01",
    )


def _evaluation() -> EvaluationSpec:
    return EvaluationSpec(
        train_end=date(2023, 12, 29),
        validation_end=date(2024, 12, 31),
        test_end=date(2025, 6, 30),
        benchmark="000300.SH",
    )


def _common() -> dict[str, object]:
    return {
        "title": "QE4 固定模板",
        "rebalance": "monthly",
        "max_positions": 2,
        "max_position_weight": 0.5,
        "cash_buffer_weight": 0.0,
        "costs": _costs(),
        "risk": RiskSpec(max_drawdown_stop=0.2, max_turnover=12.0),
        "evaluation": _evaluation(),
    }


def _direct_source() -> StrategyTemplateSource:
    research, snapshot, _ = _chain()
    return StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=("600519.SH", "000858.SZ", "000001.SZ"),
    )


def _similarity_source() -> StrategyTemplateSource:
    research, snapshot, similarity = _chain()
    return StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        similarity_run=similarity,
    )


def _limits() -> ResourceLimits:
    return ResourceLimits(
        timeout_seconds=60.0,
        max_stdout_bytes=1_048_576,
        max_stderr_bytes=1_048_576,
        memory_bytes=268_435_456,
    )


def _engine() -> EngineIdentitySpec:
    return EngineIdentitySpec(name="quantaxis", commit=_QA_COMMIT)


def test_nl01_direct_top_n_template_builds_fixed_valid_strategy() -> None:
    build = build_strategy_template(
        TopNRebalanceTemplate(
            **_common(),
            ranking_field="momentum_20d",
            ranking_direction="descending",
            top_n=2,
        ),
        source=_direct_source(),
        created_at=_CREATED_AT,
    )
    spec = build.strategy_object.payload

    assert build.template_id == "top_n_rebalance"
    assert build.source_mode == "direct"
    assert spec.similarity_run_ref is None
    assert spec.universe_symbols == ("600519.SH", "000858.SZ", "000001.SZ")
    assert spec.signals[0].model_dump() == {
        "field": "close",
        "operator": "gt",
        "value": 0.0,
        "lookback_days": 1,
        "consecutive_days": 1,
    }
    assert spec.ranking.field == "momentum_20d"
    assert spec.ranking.top_n == 2


def test_nl01_similarity_threshold_template_preserves_ranked_candidates() -> None:
    source = _similarity_source()
    build = build_strategy_template(
        FactorThresholdTemplate(
            **_common(),
            factor_field="volatility_20d",
            operator="lte",
            threshold=0.25,
        ),
        source=source,
        created_at=_CREATED_AT,
    )
    spec = build.strategy_object.payload

    assert build.source_mode == "similarity_run"
    assert spec.similarity_run_ref == source.similarity_run.ref()
    assert spec.universe_symbols == ("000858.SZ", "000001.SZ")
    assert spec.signals[0].field == "volatility_20d"
    assert spec.signals[0].lookback_days == 20
    assert spec.ranking is None


def test_nl01_factor_trend_template_is_exactly_two_allowlisted_rules() -> None:
    build = build_strategy_template(
        FactorTrendConfirmationTemplate(
            **_common(),
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.0,
            trend_kind="ema",
            trend_window=60,
        ),
        source=_direct_source(),
    )
    spec = build.strategy_object.payload

    assert tuple(
        (rule.field, rule.operator, rule.value, rule.lookback_days)
        for rule in spec.signals
    ) == (
        ("momentum_20d", "gt", 0.0, 20),
        ("close", "crosses_above", "ema_60", 1),
    )


def test_sc01_compile_is_idempotent_and_contains_complete_audit_data() -> None:
    source = _similarity_source()
    build = build_strategy_template(
        FactorThresholdTemplate(
            **_common(),
            factor_field="drawdown_20d",
            operator="gte",
            threshold=-0.15,
        ),
        source=source,
        created_at=datetime(2026, 7, 29, 8, 1, tzinfo=timezone.utc),
    )

    first = compile_strategy_template(
        build,
        snapshot=source.snapshot,
        engine=_engine(),
        resource_limits=_limits(),
        random_seed=7,
        created_at=datetime(2026, 7, 29, 8, 2, tzinfo=timezone.utc),
    )
    retry = compile_strategy_template(
        build,
        snapshot=source.snapshot,
        engine=_engine(),
        resource_limits=_limits(),
        random_seed=7,
        created_at=datetime(2026, 7, 29, 9, 2, tzinfo=timezone.utc),
    )

    assert first.plan == retry.plan
    assert first.engine_request.object_id == retry.engine_request.object_id
    assert first.plan.schema_version == STRATEGY_PLAN_VERSION
    assert first.plan.dsl_version == STRATEGY_DSL_VERSION
    assert first.plan.template_version == STRATEGY_TEMPLATE_VERSION
    assert first.plan.strategy == build.strategy_object.payload
    assert first.plan.strategy.costs == _costs()
    assert first.plan.strategy.risk.max_drawdown_stop == 0.2
    assert first.engine_request.payload.request_id.startswith(
        "qe4:factor_threshold:"
    )
    assert first.engine_request.payload.operation == "backtest"


def test_sc01_compiler_resolves_every_field_without_worker_or_code_payload() -> None:
    source = _direct_source()
    build = build_strategy_template(
        FactorTrendConfirmationTemplate(
            **_common(),
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.0,
            trend_kind="ma",
            trend_window=20,
        ),
        source=source,
    )
    compilation = compile_strategy_template(
        build,
        snapshot=source.snapshot,
        engine=_engine(),
        resource_limits=_limits(),
        random_seed=0,
    )

    assert tuple(
        (item.field_id, item.source)
        for item in compilation.plan.field_bindings
    ) == (
        ("close", "snapshot"),
        ("ma_20", "quantaxis"),
        ("momentum_20d", "qe3_factor"),
    )
    serialized = compilation.plan.model_dump_json()
    assert "python" not in serialized
    assert "subprocess" not in serialized
    assert "generated_code" not in serialized


def test_sc01_semantic_change_changes_plan_and_request_identity() -> None:
    source = _direct_source()
    base = _common()
    first_build = build_strategy_template(
        FactorThresholdTemplate(
            **base,
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.0,
        ),
        source=source,
    )
    second_build = build_strategy_template(
        FactorThresholdTemplate(
            **base,
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.1,
        ),
        source=source,
    )

    first = compile_strategy_template(
        first_build,
        snapshot=source.snapshot,
        engine=_engine(),
        resource_limits=_limits(),
        random_seed=7,
    )
    second = compile_strategy_template(
        second_build,
        snapshot=source.snapshot,
        engine=_engine(),
        resource_limits=_limits(),
        random_seed=7,
    )

    assert first.plan.plan_id != second.plan.plan_id
    assert first.engine_request.object_id != second.engine_request.object_id


def test_sc02_unknown_field_and_invalid_allocation_fail_before_compile() -> None:
    source = _direct_source()
    with pytest.raises(StrategyTemplateError, match="not allowlisted"):
        build_strategy_template(
            FactorThresholdTemplate(
                **_common(),
                factor_field="future_return_20d",
                operator="gt",
                threshold=0.0,
            ),
            source=source,
        )

    with pytest.raises(StrategyTemplateError, match="not a factor"):
        build_strategy_template(
            FactorThresholdTemplate(
                **_common(),
                factor_field="close",
                operator="gt",
                threshold=0.0,
            ),
            source=source,
        )

    with pytest.raises(ValidationError, match="valid number|valid integer"):
        FactorThresholdTemplate(
            **_common(),
            factor_field="momentum_20d",
            operator="gt",
            threshold=True,
        )

    invalid = _common()
    invalid.update(max_positions=2, max_position_weight=0.1)
    with pytest.raises(StrategySemanticError, match="allocation_infeasible"):
        build_strategy_template(
            FactorThresholdTemplate(
                **invalid,
                factor_field="momentum_20d",
                operator="gt",
                threshold=0.0,
            ),
            source=source,
        )


def test_sc02_source_chain_rejects_mixed_snapshot_and_ambiguous_universe() -> None:
    research, snapshot, similarity = _chain()
    raw = snapshot.payload.model_copy(update={"snapshot_sha256": "2" * 64})
    other_snapshot = create_research_object(
        raw,
        parent_refs=(research.ref(),),
        created_at=_CREATED_AT,
    )

    with pytest.raises(ValidationError, match="different data snapshot"):
        StrategyTemplateSource(
            research=research,
            snapshot=other_snapshot,
            similarity_run=similarity,
        )
    with pytest.raises(ValidationError, match="derives its universe"):
        StrategyTemplateSource(
            research=research,
            snapshot=snapshot,
            similarity_run=similarity,
            universe_symbols=("600519.SH",),
        )


def test_sc02_compiler_revalidates_snapshot_identity() -> None:
    source = _direct_source()
    build = build_strategy_template(
        FactorThresholdTemplate(
            **_common(),
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.0,
        ),
        source=source,
    )
    research, snapshot, _ = _chain()
    other_snapshot = create_research_object(
        snapshot.payload.model_copy(update={"snapshot_sha256": "3" * 64}),
        parent_refs=(research.ref(),),
    )

    with pytest.raises(StrategySemanticError, match="snapshot_ref_mismatch"):
        compile_strategy_template(
            build,
            snapshot=other_snapshot,
            engine=_engine(),
            resource_limits=_limits(),
            random_seed=7,
        )


def test_sc03_build_shape_cannot_be_relabelled_as_another_template() -> None:
    build = build_strategy_template(
        FactorThresholdTemplate(
            **_common(),
            factor_field="momentum_20d",
            operator="gt",
            threshold=0.0,
        ),
        source=_direct_source(),
    )

    with pytest.raises(ValidationError, match="fixed shape"):
        StrategyTemplateBuild(
            template_id="top_n_rebalance",
            source_mode=build.source_mode,
            strategy_object=build.strategy_object,
        )
