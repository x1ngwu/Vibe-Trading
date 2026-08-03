"""QE6-5 worker release transition and historical replay observability."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.quant_engine import (
    VNPY_ENGINE_COMMIT,
    VNPY_ENGINE_VERSION,
    VNPY_SOURCE_SHA256,
    OracleGovernanceStore,
    ReconciliationAccountState,
    ReconciliationCheckpoint,
    ReconciliationEngineIdentity,
    ReconciliationObservation,
    audit_oracle_replay,
    create_reconciliation_artifact,
    historical_run_identity,
)
from src.research.contracts import canonical_sha256
from tests.test_qe5_backtest_run import _run_chain


NOW = datetime(2026, 8, 3, 7, 45, tzinfo=timezone.utc)


def _vnpy_current() -> ReconciliationEngineIdentity:
    return ReconciliationEngineIdentity(
        name="vnpy",
        version=VNPY_ENGINE_VERSION,
        commit=VNPY_ENGINE_COMMIT,
        source_sha256=VNPY_SOURCE_SHA256,
    )


def _vnpy_candidate() -> ReconciliationEngineIdentity:
    return ReconciliationEngineIdentity(
        name="vnpy",
        version="4.5.0",
        commit="d" * 40,
        source_sha256={"candidate": "e" * 64},
    )


def _checkpoint(*, diverged: bool = False) -> ReconciliationCheckpoint:
    event_input = {"event": "mark", "mark_prices_fen": {}}
    qe5_state = ReconciliationAccountState(
        cash_fen=1_000_000,
        dividend_receivable_fen=0,
        positions={},
        sellable_positions={},
        mark_prices_fen={},
        market_value_fen=0,
        equity_fen=1_000_000,
    )
    vnpy_state = qe5_state.model_copy(
        update={"cash_fen": 999_999, "equity_fen": 999_999}
    ) if diverged else qe5_state
    return ReconciliationCheckpoint(
        sequence=1,
        trade_date=date(2025, 1, 2),
        event="mark",
        event_input=event_input,
        event_input_sha256=canonical_sha256(event_input),
        rule_version="cn-equity-2025-01-01",
        qe5=ReconciliationObservation(
            event_output={"status": "applied"},
            account_state=qe5_state,
        ),
        vnpy=ReconciliationObservation(
            event_output={"status": "applied"},
            account_state=vnpy_state,
        ),
    )


def _artifact(record, oracle, *, diverged: bool = False, snapshot_sha256: str | None = None):
    historical = historical_run_identity(record)
    return create_reconciliation_artifact(
        owner_scope=historical.owner_scope,
        stream_id=historical.strategy_stream_id,
        strategy_version_id=historical.strategy_version_id,
        engine_request_id=record.provenance.engine_request_ref.object_id,
        engine_request_sha256=historical.engine_request_sha256,
        execution_plan_sha256=historical.execution_plan_sha256,
        snapshot_sha256=snapshot_sha256 or historical.snapshot_sha256,
        qe5_backtest_input_sha256=historical.backtest_input_sha256,
        qe5_ledger_sha256=historical.ledger_sha256,
        vnpy_replay_input_sha256="f" * 64,
        qe5_engine=historical.original_engine,
        vnpy_engine=oracle,
        checkpoints=(_checkpoint(diverged=diverged),),
        created_at=NOW,
    )


def test_qe6_5_upgrade_records_observed_worker_release_mismatch(tmp_path: Path) -> None:
    prepared, *_ = _run_chain(tmp_path / "fixture")
    audit = audit_oracle_replay(
        prepared.record,
        mode="upgrade",
        previous_oracle=_vnpy_current(),
        expected_oracle=_vnpy_candidate(),
        observed_oracle=_vnpy_current(),
        created_at=NOW,
    )
    assert audit.status == "blocked"
    assert audit.code == "ENGINE_RELEASE_MISMATCH"
    assert audit.reconciliation_ref is None
    assert audit.historical_run.run_id == prepared.record.run_id


def test_qe6_5_rollback_and_old_run_replay_preserve_exact_lineage(tmp_path: Path) -> None:
    prepared, *_ = _run_chain(tmp_path / "fixture")
    original_json = prepared.record.model_dump_json()
    current = _vnpy_current()
    artifact = _artifact(prepared.record, current)

    rollback = audit_oracle_replay(
        prepared.record,
        mode="rollback",
        previous_oracle=_vnpy_candidate(),
        expected_oracle=current,
        observed_oracle=current,
        reconciliation=artifact,
        created_at=NOW,
    )
    replay = audit_oracle_replay(
        prepared.record,
        mode="historical_replay",
        expected_oracle=current,
        observed_oracle=current,
        reconciliation=artifact,
        created_at=NOW,
    )

    assert rollback.status == replay.status == "matched"
    assert rollback.code == replay.code == "INDEPENDENT_ORACLE_MATCH"
    assert rollback.reconciliation_ref == artifact.ref()
    assert replay.historical_run.run_content_sha256 == prepared.record.content_sha256
    assert prepared.record.model_dump_json() == original_json


def test_qe6_5_historical_divergence_and_lineage_drift_are_observable(
    tmp_path: Path,
) -> None:
    prepared, *_ = _run_chain(tmp_path / "fixture")
    current = _vnpy_current()
    diverged = audit_oracle_replay(
        prepared.record,
        mode="historical_replay",
        expected_oracle=current,
        observed_oracle=current,
        reconciliation=_artifact(prepared.record, current, diverged=True),
        created_at=NOW,
    )
    drifted = audit_oracle_replay(
        prepared.record,
        mode="historical_replay",
        expected_oracle=current,
        observed_oracle=current,
        reconciliation=_artifact(
            prepared.record,
            current,
            snapshot_sha256="0" * 64,
        ),
        created_at=NOW,
    )
    assert diverged.status == "diverged"
    assert diverged.code == "FIRST_DIVERGENCE"
    assert drifted.status == "blocked"
    assert drifted.code == "ARTIFACT_LINEAGE_MISMATCH"


def test_qe6_5_not_available_is_not_reported_as_passed(tmp_path: Path) -> None:
    prepared, *_ = _run_chain(tmp_path / "fixture")
    audit = audit_oracle_replay(
        prepared.record,
        mode="historical_replay",
        expected_oracle=_vnpy_candidate(),
        capability_available=False,
        created_at=NOW,
    )
    assert audit.status == "not_available"
    assert audit.code == "CAPABILITY_NOT_AVAILABLE"
    assert audit.observed_oracle is None
    assert audit.reconciliation_ref is None


def test_qe6_5_governance_store_persists_evidence_closure(tmp_path: Path) -> None:
    prepared, *_ = _run_chain(tmp_path / "fixture")
    current = _vnpy_current()
    artifact = _artifact(prepared.record, current)
    audit = audit_oracle_replay(
        prepared.record,
        mode="historical_replay",
        expected_oracle=current,
        observed_oracle=current,
        reconciliation=artifact,
        created_at=NOW,
    )
    store = OracleGovernanceStore(tmp_path / "governance")
    assert store.put_artifact(artifact) is True
    assert store.put_audit(audit, record=prepared.record) is True
    assert store.put_audit(audit, record=prepared.record) is False
    assert store.get_audit(
        audit.audit_id,
        owner_scope="household:v1",
        record=prepared.record,
    ) == audit
    assert store.get_audit(audit.audit_id, owner_scope="household:other") is None
