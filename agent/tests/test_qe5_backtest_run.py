"""QE5-3 normalized BacktestRun, provenance, idempotency, and lineage tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import stat

import pytest

from tests.test_qe5_quantaxis_backtest import (
    _DraftModel,
    _T0,
    _WorkerRunner,
    _confirmed_chain,
)
from src.quant_engine import (
    BacktestRunError,
    BacktestRunIdempotencyConflict,
    BacktestRunIntegrityError,
    BacktestRunStore,
    NormalizedBacktestRecord,
    QuantaxisAdapter,
    normalize_quantaxis_backtest_run,
)
from src.research.contracts import (
    ResearchSpec,
    canonical_sha256,
    create_research_object,
)
from src.research.store import ResearchStore, StoreIntegrityError


def _run_chain(
    tmp_path: Path,
    *,
    initial_cash_fen: int = 1_000_000,
    draft_model: _DraftModel | None = None,
):
    snapshot_path, snapshot, compilation, version, head, card, receipt = (
        _confirmed_chain(tmp_path, draft_model=draft_model)
    )
    result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        initial_cash_fen=initial_cash_fen,
    )
    prepared = normalize_quantaxis_backtest_run(
        result,
        compilation=compilation,
        version=version,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        created_at=_T0 + timedelta(minutes=3),
    )
    return prepared, result, snapshot, compilation, version, head, card, receipt


def _store_parents(
    store: ResearchStore,
    *,
    snapshot,
    compilation,
    version,
) -> None:
    research = create_research_object(
        ResearchSpec(
            symbols=("600001.SH",),
            as_of=date(2025, 1, 7),
            lookback_days=(20,),
            candidate_universe="qe5-worker-fixture",
            requested_outputs=("strategy", "backtest"),
        ),
        created_at=_T0,
    )
    strategy = create_research_object(
        version.strategy,
        owner_scope=version.owner_scope,
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=_T0,
    )
    assert research.ref() == snapshot.parent_refs[0]
    assert strategy.ref() == version.strategy_spec_ref
    for item in (research, snapshot, strategy, compilation.engine_request):
        store.put(item)


def test_qe5_3_normalizes_complete_reconciled_run_with_exact_provenance(
    tmp_path: Path,
) -> None:
    prepared, result, snapshot, compilation, version, _head, card, receipt = (
        _run_chain(tmp_path)
    )
    record = prepared.record

    assert record.status == "completed"
    assert record.ledger == result.ledger
    assert record.ledger_sha256 == result.ledger.content_sha256
    assert record.provenance.strategy_version_id == version.version_id
    assert record.provenance.parent_strategy_version_id is None
    assert record.provenance.confirmation_card_id == card.card_id
    assert record.provenance.confirmation_receipt_id == receipt.receipt_id
    assert record.provenance.execution_plan_sha256 == compilation.plan.content_sha256
    assert record.provenance.snapshot_sha256 == snapshot.payload.snapshot_sha256
    assert record.provenance.actual_sources == snapshot.payload.actual_sources
    assert len(record.fills) == record.metrics.trade_count == 3
    assert len(record.fee_entries) == len(record.fills)
    assert len(record.cash_entries) == len(record.ledger.entries)
    assert len(record.position_snapshots) == len(record.daily_equity) == 4
    assert {item.kind for item in record.diagnostics} == {"signal", "risk"}
    assert prepared.object.payload.ledger_sha256 == record.ledger_sha256
    assert prepared.object.payload.artifact_refs == (record.artifact_ref,)


def test_qe5_3_rejections_are_normalized_without_accounting_effect(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(
        tmp_path,
        draft_model=_DraftModel(max_turnover=0.11),
    )

    assert prepared.record.rejections
    assert {item.kind for item in prepared.record.diagnostics} == {
        "rejection",
        "signal",
        "risk",
    }
    for rejection in prepared.record.rejections:
        ledger_entry = prepared.record.ledger.entries[rejection.sequence]
        assert ledger_entry.event == "rejected_order"
        assert ledger_entry.cash_delta_fen == 0
        assert ledger_entry.position_delta == {}
        assert ledger_entry.fees.total_fen == 0


def test_qe5_3_normalization_is_content_deterministic_across_audit_timestamps(
    tmp_path: Path,
) -> None:
    prepared, result, snapshot, compilation, version, _head, card, receipt = (
        _run_chain(tmp_path)
    )
    replay = normalize_quantaxis_backtest_run(
        result,
        compilation=compilation,
        version=version,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        created_at=_T0 + timedelta(days=1),
    )

    assert replay.record == prepared.record
    assert replay.object.ref() == prepared.object.ref()
    assert replay.object.created_at != prepared.object.created_at


def test_qe5_3_normalizer_recomputes_exact_worker_input_identity(
    tmp_path: Path,
) -> None:
    _prepared, result, snapshot, compilation, version, _head, card, receipt = (
        _run_chain(tmp_path)
    )
    forged = replace(
        result,
        worker=result.worker.model_copy(
            update={"backtest_input_sha256": "0" * 64},
        ),
    )

    with pytest.raises(BacktestRunError, match="input identity"):
        normalize_quantaxis_backtest_run(
            forged,
            compilation=compilation,
            version=version,
            card=card,
            receipt=receipt,
            snapshot=snapshot,
        )


def test_qe5_3_recomputed_record_identity_cannot_hide_projection_tampering(
    tmp_path: Path,
) -> None:
    prepared, *_rest = _run_chain(tmp_path)
    raw = prepared.record.model_dump(mode="json")
    raw["cash_entries"][-1]["cash_fen"] += 1
    material = {
        key: value
        for key, value in raw.items()
        if key not in {"run_id", "content_sha256"}
    }
    digest = canonical_sha256(material)
    raw["content_sha256"] = digest
    raw["run_id"] = f"backtest-record:{digest}"

    with pytest.raises(ValueError, match="cash entries do not match"):
        NormalizedBacktestRecord.model_validate(raw)


def test_qe5_3_store_persists_record_summary_and_idempotent_aliases(
    tmp_path: Path,
) -> None:
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )

    first = store.put(prepared, idempotency_key="run-click-1")
    retry = store.put(prepared, idempotency_key="run-click-1")
    alias = store.put(prepared, idempotency_key="run-retry-2")

    assert first.created is True
    assert retry.created is False
    assert alias.created is False
    assert retry.record == alias.record == first.record
    assert retry.object == alias.object == first.object
    assert store.get_by_idempotency_key(
        owner_scope=version.owner_scope,
        idempotency_key="run-click-1",
    ) == retry
    assert store.get(
        prepared.record.run_id,
        owner_scope="household:other",
    ) is None
    assert store.list_for_strategy_version(
        version.version_id,
        owner_scope=version.owner_scope,
    ) == (retry,)
    assert store.list_for_strategy_stream(
        version.stream_id,
        owner_scope=version.owner_scope,
    ) == (retry,)

    reopened = BacktestRunStore(
        tmp_path / "backtests",
        research_store=ResearchStore(tmp_path / "research"),
    )
    restored = reopened.get(
        prepared.record.run_id,
        owner_scope=version.owner_scope,
    )
    assert restored is not None
    assert restored.record == prepared.record
    assert restored.object == first.object


def test_qe5_3_store_secures_files_and_rejects_symlink_database(
    tmp_path: Path,
) -> None:
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )
    store.put(prepared, idempotency_key="secure-files")

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.records_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600
    record_path = store.records_dir / f"{prepared.record.content_sha256}.json"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    for sidecar in (
        Path(f"{store.database_path}-wal"),
        Path(f"{store.database_path}-shm"),
    ):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    linked_root = tmp_path / "linked-backtests"
    linked_root.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    (linked_root / "backtest-runs.db").symlink_to(outside)
    with pytest.raises(BacktestRunIntegrityError, match="symlink"):
        BacktestRunStore(
            linked_root,
            research_store=research_store,
        )


def test_qe5_3_idempotency_key_reuse_with_other_initial_cash_fails(
    tmp_path: Path,
) -> None:
    prepared, _result, snapshot, compilation, version, head, card, receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )
    store.put(prepared, idempotency_key="same-key")

    changed_result = QuantaxisAdapter(_WorkerRunner()).backtest(  # type: ignore[arg-type]
        compilation=compilation,
        version=version,
        head=head,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        snapshot_path=tmp_path / "fixture" / "qe5-backtest-snapshot.json",
        initial_cash_fen=2_000_000,
    )
    changed = normalize_quantaxis_backtest_run(
        changed_result,
        compilation=compilation,
        version=version,
        card=card,
        receipt=receipt,
        snapshot=snapshot,
        created_at=_T0 + timedelta(minutes=4),
    )
    with pytest.raises(
        BacktestRunIdempotencyConflict,
        match="another BacktestRun",
    ):
        store.put(changed, idempotency_key="same-key")


def test_qe5_3_missing_parent_fails_but_retry_recovers_orphan_record(
    tmp_path: Path,
) -> None:
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )

    with pytest.raises(StoreIntegrityError, match="parent object is not stored"):
        store.put(prepared, idempotency_key="recover-after-parent")
    record_path = (
        store.records_dir / f"{prepared.record.content_sha256}.json"
    )
    assert record_path.is_file()
    assert store.get_by_idempotency_key(
        owner_scope=version.owner_scope,
        idempotency_key="recover-after-parent",
    ) is None

    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    recovered = store.put(
        prepared,
        idempotency_key="recover-after-parent",
    )
    assert recovered.created is True
    assert recovered.record == prepared.record


def test_qe5_3_indexed_missing_record_fails_closed(
    tmp_path: Path,
) -> None:
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )
    store.put(prepared, idempotency_key="missing-record")
    path = store.records_dir / f"{prepared.record.content_sha256}.json"
    path.unlink()

    with pytest.raises(BacktestRunIntegrityError, match="index references"):
        store.get_by_idempotency_key(
            owner_scope=version.owner_scope,
            idempotency_key="missing-record",
        )
    with pytest.raises(BacktestRunIntegrityError, match="index references"):
        store.list_for_strategy_version(
            version.version_id,
            owner_scope=version.owner_scope,
        )


def test_qe5_3_tampered_record_fails_closed_on_restore(tmp_path: Path) -> None:
    prepared, _result, snapshot, compilation, version, _head, _card, _receipt = (
        _run_chain(tmp_path / "fixture")
    )
    research_store = ResearchStore(tmp_path / "research")
    _store_parents(
        research_store,
        snapshot=snapshot,
        compilation=compilation,
        version=version,
    )
    store = BacktestRunStore(
        tmp_path / "backtests",
        research_store=research_store,
    )
    store.put(prepared, idempotency_key="tamper-test")
    path = store.records_dir / f"{prepared.record.content_sha256}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["daily_equity"][-1]["equity_fen"] += 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        BacktestRunIntegrityError,
        match="invalid normalized BacktestRun",
    ):
        store.get(
            prepared.record.run_id,
            owner_scope=version.owner_scope,
        )
