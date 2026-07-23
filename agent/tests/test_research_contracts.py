"""QE1 CT-01/CT-02 tests for strict, content-addressed research objects."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research.contracts import (
    BacktestMetrics,
    BacktestRun,
    ChannelWeights,
    ContractError,
    CostSpec,
    DataSnapshotRef,
    EngineIdentitySpec,
    EngineRequest,
    EvaluationSpec,
    ExecutionSpec,
    FactorEvidence,
    FactorObservation,
    ObjectRef,
    PeerSet,
    PortfolioSpec,
    RankingSpec,
    ResearchObject,
    ResearchReport,
    ResearchSpec,
    ResourceLimits,
    RiskSpec,
    SignalRule,
    SimilarityRun,
    StockCandidate,
    StrategySpec,
    canonical_json,
    canonical_sha256,
    create_research_object,
)
from src.research.graph import ResearchGraphError, validate_research_chain
from src.research.migrations import MigrationError, migrate_research_object

_QA_COMMIT = "a69e978a2e38d045a64c380cc3b5c9fa08fa4903"
_FIXTURE = Path(__file__).parent / "fixtures" / "research" / "qe1_research_spec_v1.json"
_LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "research" / "qe1_research_spec_v0_9.json"


def _qe1_object_chain() -> tuple[ResearchObject, ...]:
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH", "000858.SZ"),
            as_of=date(2025, 6, 30),
            lookback_days=(60, 120, 252),
            candidate_universe="csi300@2025-06-30",
        ),
        created_at=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="1" * 64,
            as_of=date(2025, 6, 30),
            start_date=date(2024, 6, 3),
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
        created_at=datetime(2026, 7, 23, 0, 1, tzinfo=timezone.utc),
    )
    peers = create_research_object(
        PeerSet(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            target_symbol="600519.SH",
            members=("000858.SZ", "000001.SZ"),
            included_reasons={
                "000858.SZ": ("same-industry", "similar-market-cap"),
                "000001.SZ": ("liquidity-control",),
            },
            excluded_reasons={"600519.SH": ("self",)},
            coverage=1.0,
        ),
        parent_refs=(snapshot.ref(), research.ref()),
        created_at=datetime(2026, 7, 23, 0, 2, tzinfo=timezone.utc),
    )
    evidence = create_research_object(
        FactorEvidence(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            peer_set_refs=(peers.ref(),),
            factor_id="momentum.120d",
            direction="positive",
            observations=(
                FactorObservation(
                    symbol="600519.SH",
                    value=0.18,
                    peer_median=0.05,
                    robust_zscore=1.4,
                    percentile=0.9,
                    source_fields=("close",),
                ),
            ),
            supporting_symbols=("600519.SH",),
            contradicting_symbols=(),
            coverage=1.0,
            stability=0.8,
        ),
        parent_refs=(research.ref(), snapshot.ref(), peers.ref()),
        created_at=datetime(2026, 7, 23, 0, 3, tzinfo=timezone.utc),
    )
    similarity = create_research_object(
        SimilarityRun(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            factor_evidence_refs=(evidence.ref(),),
            weights=ChannelWeights(business=0.3, factor=0.4, price_volume=0.3),
            candidates=(
                StockCandidate(
                    symbol="000858.SZ",
                    rank=1,
                    business_score=0.9,
                    factor_score=0.8,
                    price_volume_score=0.7,
                    combined_score=0.8,
                    coverage=1.0,
                    evidence=("same-industry", "similar-momentum"),
                    counterevidence=("higher-volatility",),
                ),
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref(), evidence.ref()),
        created_at=datetime(2026, 7, 23, 0, 4, tzinfo=timezone.utc),
    )
    strategy = create_research_object(
        StrategySpec(
            research_spec_ref=research.ref(),
            similarity_run_ref=similarity.ref(),
            data_snapshot_ref=snapshot.ref(),
            title="Monthly momentum top one",
            universe_symbols=("000858.SZ",),
            signals=(
                SignalRule(
                    field="momentum.120d",
                    operator="gt",
                    value=0.0,
                    lookback_days=120,
                ),
            ),
            ranking=RankingSpec(field="momentum.120d", direction="descending", top_n=1),
            portfolio=PortfolioSpec(max_positions=1, max_position_weight=0.95, cash_buffer_weight=0.05),
            execution=ExecutionSpec(rebalance="monthly"),
            costs=CostSpec(
                commission_bps=3.0,
                minimum_commission=5.0,
                sell_tax_bps=5.0,
                transfer_fee_bps=0.1,
                slippage_bps=5.0,
                rule_version="cn-equity-2025-01-01",
            ),
            risk=RiskSpec(max_drawdown_stop=0.2, max_turnover=12.0),
            evaluation=EvaluationSpec(
                train_end=date(2023, 12, 29),
                validation_end=date(2024, 12, 31),
                test_end=date(2025, 6, 30),
                benchmark="000300.SH",
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref(), similarity.ref()),
        created_at=datetime(2026, 7, 23, 0, 5, tzinfo=timezone.utc),
    )
    request = create_research_object(
        EngineRequest(
            request_id="qe1-fixture-request",
            strategy_spec_ref=strategy.ref(),
            data_snapshot_ref=snapshot.ref(),
            engine=EngineIdentitySpec(name="quantaxis", commit=_QA_COMMIT),
            operation="backtest",
            resource_limits=ResourceLimits(
                timeout_seconds=30.0,
                max_stdout_bytes=1_048_576,
                max_stderr_bytes=1_048_576,
                memory_bytes=268_435_456,
            ),
            random_seed=7,
        ),
        parent_refs=(strategy.ref(), snapshot.ref()),
        created_at=datetime(2026, 7, 23, 0, 6, tzinfo=timezone.utc),
    )
    run = create_research_object(
        BacktestRun(
            engine_request_ref=request.ref(),
            strategy_spec_ref=strategy.ref(),
            data_snapshot_ref=snapshot.ref(),
            engine=EngineIdentitySpec(name="quantaxis", commit=_QA_COMMIT),
            status="completed",
            ledger_sha256="2" * 64,
            metrics=BacktestMetrics(
                total_return=0.12,
                annualized_return=0.1,
                max_drawdown=-0.08,
                turnover=2.5,
                trade_count=4,
            ),
            artifact_refs=("ledger.jsonl", "daily_equity.csv"),
        ),
        parent_refs=(request.ref(), strategy.ref(), snapshot.ref()),
        created_at=datetime(2026, 7, 23, 0, 7, tzinfo=timezone.utc),
    )
    report = create_research_object(
        ResearchReport(
            research_spec_ref=research.ref(),
            evidence_refs=(evidence.ref(),),
            similarity_run_ref=similarity.ref(),
            strategy_spec_refs=(strategy.ref(),),
            backtest_run_refs=(run.ref(),),
            title="QE1 fixed research report",
            summary="A deterministic report built only from referenced objects.",
            limitations=("synthetic fixture",),
            visualization_refs=("fixture-equity-curve",),
        ),
        parent_refs=(research.ref(), evidence.ref(), similarity.ref(), strategy.ref(), run.ref()),
        created_at=datetime(2026, 7, 23, 0, 8, tzinfo=timezone.utc),
    )
    return research, snapshot, peers, evidence, similarity, strategy, request, run, report


def test_ct01_all_public_objects_strict_json_round_trip() -> None:
    objects = _qe1_object_chain()

    assert len(objects) == 9
    assert {item.object_type for item in objects} == {
        "research_spec",
        "data_snapshot_ref",
        "peer_set",
        "factor_evidence",
        "similarity_run",
        "strategy_spec",
        "engine_request",
        "backtest_run",
        "research_report",
    }
    for item in objects:
        assert ResearchObject.model_validate_json(item.model_dump_json()) == item
        assert type(item.payload).model_validate_json(item.payload.model_dump_json()) == item.payload
        assert item.ref().object_id == item.object_id


def test_ct01_committed_v1_fixture_round_trips_without_rehashing() -> None:
    raw = _FIXTURE.read_text(encoding="utf-8")
    loaded = ResearchObject.model_validate_json(raw)

    assert loaded.object_type == "research_spec"
    assert loaded.content_sha256 == "2d9230b693c2b5f9be57ed6e2fd9ffc7ae87a6e301bad7ad53d026c0a57d786f"
    assert ResearchObject.model_validate_json(loaded.model_dump_json()) == loaded


def test_ct01_generated_json_schema_is_strict_and_discriminated() -> None:
    schema = ResearchObject.model_json_schema()

    assert schema["additionalProperties"] is False
    payload_schema = schema["properties"]["payload"]
    assert payload_schema["discriminator"]["propertyName"] == "object_type"
    assert set(payload_schema["discriminator"]["mapping"]) == {
        "research_spec",
        "data_snapshot_ref",
        "peer_set",
        "factor_evidence",
        "similarity_run",
        "strategy_spec",
        "engine_request",
        "backtest_run",
        "research_report",
    }


def test_ct03_committed_legacy_fixture_migrates_deterministically() -> None:
    raw = json.loads(_LEGACY_FIXTURE.read_text(encoding="utf-8"))
    first = migrate_research_object(raw)
    second = migrate_research_object(dict(reversed(tuple(raw.items()))))

    assert first.source_version == "0.9"
    assert first.target_version == "1.0"
    assert first.source_sha256 == second.source_sha256
    assert first.object == second.object
    assert first.object.payload.object_type == "research_spec"
    assert first.object.payload.lookback_days == (120,)
    assert first.object.owner_scope == "household:v1"


def test_ct03_migration_rejects_unknown_versions_and_legacy_fields() -> None:
    raw = json.loads(_LEGACY_FIXTURE.read_text(encoding="utf-8"))
    unsupported = dict(raw)
    unsupported["schema_version"] = "0.8"
    with pytest.raises(MigrationError, match="unsupported schema migration"):
        migrate_research_object(unsupported)

    unknown = dict(raw)
    unknown["surprise"] = True
    with pytest.raises(MigrationError, match=r"unknown=\['surprise'\]"):
        migrate_research_object(unknown)


def test_ct03_complete_object_chain_uses_one_spec_and_snapshot() -> None:
    objects = _qe1_object_chain()
    validate_research_chain(objects)

    with pytest.raises(ResearchGraphError, match="exactly one research_spec"):
        validate_research_chain(objects[1:])

    research, snapshot, *rest = objects
    second_snapshot = create_research_object(
        snapshot.payload.model_copy(update={"snapshot_sha256": "9" * 64}),
        parent_refs=(research.ref(),),
        created_at=snapshot.created_at,
    )
    with pytest.raises(ResearchGraphError, match="exactly one data_snapshot_ref"):
        validate_research_chain((research, snapshot, second_snapshot, *rest))


def test_ct01_unknown_fields_and_unknown_major_version_fail_closed() -> None:
    raw = _qe1_object_chain()[0].model_dump(mode="json")

    unknown_envelope = dict(raw)
    unknown_envelope["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchObject.model_validate(unknown_envelope)

    unknown_payload = json.loads(json.dumps(raw))
    unknown_payload["payload"]["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchObject.model_validate(unknown_payload)

    unknown_major = dict(raw)
    unknown_major["schema_version"] = "2.0"
    with pytest.raises(ValidationError, match="Input should be '1.0'"):
        ResearchObject.model_validate(unknown_major)


def test_ct01_tampered_content_and_reference_identity_fail_closed() -> None:
    raw = _qe1_object_chain()[0].model_dump(mode="json")
    raw["payload"]["candidate_universe"] = "all-a-shares"
    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        ResearchObject.model_validate(raw)

    with pytest.raises(ValidationError, match="object_id must be object_type"):
        ObjectRef(
            object_type="research_spec",
            object_id="research_spec:" + "a" * 64,
            content_sha256="b" * 64,
        )


def test_ct01_envelope_cannot_omit_explicit_payload_references() -> None:
    research, snapshot, peers, *_ = _qe1_object_chain()
    raw = peers.model_dump(mode="json")
    raw["parent_refs"] = [snapshot.ref().model_dump(mode="json")]
    material = {
        "schema_version": raw["schema_version"],
        "object_type": raw["object_type"],
        "owner_scope": raw["owner_scope"],
        "parent_refs": raw["parent_refs"],
        "payload": raw["payload"],
    }
    raw["content_sha256"] = canonical_sha256(material)
    raw["object_id"] = f"peer_set:{raw['content_sha256']}"

    with pytest.raises(ValidationError, match="parent_refs omit payload references"):
        ResearchObject.model_validate(raw)
    assert research.ref().object_id not in {ref["object_id"] for ref in raw["parent_refs"]}


def test_ct01_created_at_must_be_timezone_aware() -> None:
    payload = _qe1_object_chain()[0].payload
    with pytest.raises(ValidationError, match="timezone"):
        create_research_object(payload, created_at=datetime(2026, 7, 23, 0, 0))


def test_ct02_canonical_json_ignores_mapping_input_order() -> None:
    first = {"z": [3, 2, 1], "a": {"right": 2, "left": 1}, "text": "量化"}
    second = {"text": "量化", "a": {"left": 1, "right": 2}, "z": [3, 2, 1]}

    assert canonical_json(first) == canonical_json(second)
    assert canonical_sha256(first) == canonical_sha256(second)


def test_ct02_content_identity_is_retry_stable_but_semantic_changes_rehash() -> None:
    research, snapshot, *_ = _qe1_object_chain()
    earlier = create_research_object(
        snapshot.payload,
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc),
    )
    later = create_research_object(
        snapshot.payload,
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
    )
    changed = create_research_object(
        snapshot.payload.model_copy(update={"adjustment": "raw"}),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
    )

    assert earlier.created_at != later.created_at
    assert earlier.content_sha256 == later.content_sha256
    assert earlier.object_id == later.object_id
    assert changed.content_sha256 != later.content_sha256
    assert changed.object_id != later.object_id


def test_ct02_parent_input_order_is_canonical() -> None:
    research, snapshot, peers, *_ = _qe1_object_chain()
    first = create_research_object(
        peers.payload,
        parent_refs=(snapshot.ref(), research.ref()),
        created_at=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
    )
    second = create_research_object(
        peers.payload,
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
    )
    assert first.parent_refs == second.parent_refs
    assert first.content_sha256 == second.content_sha256


def test_ct02_canonical_hash_is_process_hashseed_and_locale_independent() -> None:
    script = """
from src.research.contracts import canonical_sha256
keys = {"z", "a", "量"}
values = {"z": [3, 2, 1], "a": {"b": 2, "a": 1}, "量": "价"}
print(canonical_sha256({key: values[key] for key in keys}))
"""
    outputs: list[str] = []
    for hash_seed, locale in (("1", "C"), ("987654", "C.UTF-8")):
        env = os.environ.copy()
        env.update({"PYTHONHASHSEED": hash_seed, "LC_ALL": locale})
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 64


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_ct02_non_finite_values_are_not_canonical_json(value: float) -> None:
    with pytest.raises(ContractError, match="not canonical JSON"):
        canonical_json({"value": value})
