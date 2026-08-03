"""QE6-4 first-divergence evidence and strategy validation gate."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.quant_engine import (
    ReconciliationAccountState,
    ReconciliationArtifact,
    ReconciliationCheckpoint,
    ReconciliationEngineIdentity,
    ReconciliationIntegrityError,
    ReconciliationObservation,
    ReconciliationStore,
    StrategyValidationDecision,
    create_reconciliation_artifact,
    decide_strategy_validation,
)
from src.research.contracts import canonical_sha256


SHA = {
    "version": "1" * 64,
    "request": "2" * 64,
    "plan": "3" * 64,
    "snapshot": "4" * 64,
    "input": "5" * 64,
    "ledger": "6" * 64,
    "replay": "7" * 64,
}
CREATED_AT = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)


def _account(*, cash_fen: int = 900_000, shares: int = 100) -> ReconciliationAccountState:
    positions = {"600001.SH": shares} if shares else {}
    marks = {"600001.SH": 1_000} if shares else {}
    market_value = shares * 1_000
    return ReconciliationAccountState(
        cash_fen=cash_fen,
        dividend_receivable_fen=0,
        positions=positions,
        sellable_positions={},
        mark_prices_fen=marks,
        market_value_fen=market_value,
        equity_fen=cash_fen + market_value,
    )


def _checkpoint(
    sequence: int,
    *,
    vnpy_cash_delta: int = 0,
    event: str = "buy",
) -> ReconciliationCheckpoint:
    event_input = {
        "order_id": f"order-{sequence}",
        "symbol": "600001.SH",
        "side": "buy",
        "requested_shares": 100,
        "price_fen": 1_000,
    }
    qe5 = ReconciliationObservation(
        event_output={
            "order_id": f"order-{sequence}",
            "status": "filled",
            "filled_shares": 100,
            "fees_fen": 500,
        },
        account_state=_account(cash_fen=900_000 - sequence * 500),
    )
    vnpy = ReconciliationObservation(
        event_output=qe5.event_output,
        account_state=_account(cash_fen=900_000 - sequence * 500 + vnpy_cash_delta),
    )
    return ReconciliationCheckpoint(
        sequence=sequence,
        trade_date=date(2025, 1, 2 + sequence),
        event=event,
        event_input=event_input,
        event_input_sha256=canonical_sha256(event_input),
        rule_version="cn-equity-2025-01-01",
        qe5=qe5,
        vnpy=vnpy,
    )


def _engine(name: str) -> ReconciliationEngineIdentity:
    return ReconciliationEngineIdentity(
        name=name,
        version="2.7.2" if name == "quantaxis" else "4.4.0",
        commit=("8" if name == "quantaxis" else "9") * 40,
        source_sha256={"runtime": ("a" if name == "quantaxis" else "b") * 64},
    )


def _artifact(
    checkpoints: tuple[ReconciliationCheckpoint, ...],
    *,
    created_at: datetime = CREATED_AT,
):
    return create_reconciliation_artifact(
        owner_scope="household:v1",
        stream_id="strategy:test",
        strategy_version_id=f"strategy-version:{SHA['version']}",
        engine_request_id="backtest:test",
        engine_request_sha256=SHA["request"],
        execution_plan_sha256=SHA["plan"],
        snapshot_sha256=SHA["snapshot"],
        qe5_backtest_input_sha256=SHA["input"],
        qe5_ledger_sha256=SHA["ledger"],
        vnpy_replay_input_sha256=SHA["replay"],
        qe5_engine=_engine("quantaxis"),
        vnpy_engine=_engine("vnpy"),
        checkpoints=checkpoints,
        created_at=created_at,
    )


def test_qe6_4_matched_artifact_is_the_only_path_to_validated(tmp_path: Path) -> None:
    artifact = _artifact((_checkpoint(1), _checkpoint(2)))
    decision = decide_strategy_validation(
        artifact,
        owner_scope="household:v1",
        stream_id="strategy:test",
        strategy_version_id=f"strategy-version:{SHA['version']}",
        decided_at=CREATED_AT,
    )

    assert artifact.comparison_status == "matched"
    assert artifact.first_divergence is None
    assert artifact.checkpoints == (_checkpoint(1), _checkpoint(2))
    assert decision.status == "validated"
    assert decision.reason == "INDEPENDENT_ORACLE_MATCH"

    store = ReconciliationStore(tmp_path / "reconciliation")
    assert store.put_artifact(artifact) is True
    assert store.put_decision(decision) is True
    assert store.get_artifact(
        artifact.artifact_id, owner_scope="household:v1"
    ) == artifact
    assert store.get_decision(
        decision.decision_id, owner_scope="household:v1"
    ) == decision


def test_qe6_4_records_the_earliest_divergence_and_blocks_validation() -> None:
    first = _checkpoint(1)
    second = _checkpoint(2, vnpy_cash_delta=1)
    third = _checkpoint(3, vnpy_cash_delta=2)
    artifact = _artifact((first, second, third))
    decision = decide_strategy_validation(
        artifact,
        owner_scope="household:v1",
        stream_id="strategy:test",
        strategy_version_id=f"strategy-version:{SHA['version']}",
        decided_at=CREATED_AT,
    )

    assert artifact.comparison_status == "diverged"
    assert artifact.first_divergence is not None
    assert artifact.first_divergence.sequence == 2
    assert artifact.first_divergence.trade_date == date(2025, 1, 4)
    assert artifact.first_divergence.event_input == second.event_input
    assert artifact.first_divergence.event_input_sha256 == second.event_input_sha256
    assert artifact.first_divergence.rule_version == "cn-equity-2025-01-01"
    assert artifact.first_divergence.differing_fields == (
        "account_state.cash_fen",
        "account_state.equity_fen",
    )
    assert artifact.first_divergence.qe5.account_state.cash_fen == 899_000
    assert artifact.first_divergence.vnpy.account_state.cash_fen == 899_001
    assert decision.status == "blocked"
    assert decision.reason == "FIRST_DIVERGENCE"


def test_qe6_4_lineage_mismatch_cannot_validate_another_strategy() -> None:
    artifact = _artifact((_checkpoint(1),))
    decision = decide_strategy_validation(
        artifact,
        owner_scope="household:v1",
        stream_id="strategy:other",
        strategy_version_id=f"strategy-version:{'c' * 64}",
        decided_at=CREATED_AT,
    )
    assert decision.status == "blocked"
    assert decision.reason == "ARTIFACT_LINEAGE_MISMATCH"


def test_qe6_4_artifact_contract_rejects_false_status_and_bad_input_hash() -> None:
    divergent = _artifact((_checkpoint(1, vnpy_cash_delta=1),))
    forged = divergent.model_dump(mode="python")
    forged["comparison_status"] = "matched"
    with pytest.raises(ValidationError, match="comparison_status does not match"):
        ReconciliationArtifact.model_validate(forged)

    checkpoint = _checkpoint(1).model_dump(mode="python")
    checkpoint["event_input_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="does not match event_input"):
        ReconciliationCheckpoint.model_validate(checkpoint)


def test_qe6_4_recomputed_outer_hash_cannot_hide_rewritten_comparison() -> None:
    divergent = _artifact((_checkpoint(1, vnpy_cash_delta=1),))
    forged = divergent.model_dump(mode="python")
    forged["comparison_status"] = "matched"
    forged["first_divergence"] = None
    forged["comparison_sha256"] = "0" * 64
    material = {
        key: value
        for key, value in forged.items()
        if key not in {"artifact_id", "content_sha256", "created_at"}
    }
    digest = canonical_sha256(material)
    forged["artifact_id"] = f"reconciliation-artifact:{digest}"
    forged["content_sha256"] = digest

    with pytest.raises(ValidationError, match="comparison_sha256 does not match"):
        ReconciliationArtifact.model_validate(forged)


def test_qe6_4_evidence_rejects_numeric_string_coercion() -> None:
    account = _account().model_dump(mode="python")
    account["cash_fen"] = "900000"
    with pytest.raises(ValidationError, match="cash_fen"):
        ReconciliationAccountState.model_validate(account)

    checkpoint = _checkpoint(1).model_dump(mode="python")
    checkpoint["sequence"] = "1"
    with pytest.raises(ValidationError, match="sequence"):
        ReconciliationCheckpoint.model_validate(checkpoint)


def test_qe6_4_store_is_content_addressed_idempotent_and_detects_tampering(
    tmp_path: Path,
) -> None:
    store = ReconciliationStore(tmp_path / "reconciliation")
    artifact = _artifact((_checkpoint(1),))
    same_content_later = _artifact(
        (_checkpoint(1),),
        created_at=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
    )
    assert artifact.artifact_id == same_content_later.artifact_id
    assert store.put_artifact(artifact) is True
    assert store.put_artifact(same_content_later) is False

    path = store.artifacts_dir / f"{artifact.content_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot_sha256"] = "d" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconciliationIntegrityError, match="invalid reconciliation object"):
        store.get_artifact(artifact.artifact_id, owner_scope="household:v1")


def test_qe6_4_store_rejects_a_forged_validation_decision(tmp_path: Path) -> None:
    store = ReconciliationStore(tmp_path / "reconciliation")
    artifact = _artifact((_checkpoint(1, vnpy_cash_delta=1),))
    store.put_artifact(artifact)
    blocked = decide_strategy_validation(
        artifact,
        owner_scope="household:v1",
        stream_id="strategy:test",
        strategy_version_id=f"strategy-version:{SHA['version']}",
        decided_at=CREATED_AT,
    )
    forged_material = blocked.model_dump(
        mode="json",
        exclude={"decision_id", "content_sha256", "decided_at"},
    )
    forged_material["status"] = "validated"
    forged_material["reason"] = "INDEPENDENT_ORACLE_MATCH"
    digest = canonical_sha256(forged_material)
    forged = StrategyValidationDecision(
        decision_id=f"strategy-validation:{digest}",
        content_sha256=digest,
        decided_at=CREATED_AT,
        **forged_material,
    )
    with pytest.raises(ReconciliationIntegrityError, match="not derived"):
        store.put_decision(forged)
