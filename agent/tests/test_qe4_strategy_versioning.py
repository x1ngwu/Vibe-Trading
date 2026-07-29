"""QE4-4 immutable versions, confirmation hashes, expiry, and races."""

from __future__ import annotations

import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from src.research.contracts import DataSnapshotRef, ResearchSpec, create_research_object
from src.strategy_spec import (
    ExpiredStrategyConfirmationError,
    InvalidStrategyTransitionError,
    StaleStrategyHeadError,
    StrategyConfirmationHashMismatch,
    StrategyDraftRequest,
    StrategyIdempotencyConflict,
    StrategyVersionError,
    StrategyVersionStore,
    StrategyVersionStoreIntegrityError,
    StrategyTemplateSource,
    draft_strategy_from_language,
)
from src.strategy_spec.presentation import (
    StrategyDataBasis,
    build_strategy_visualization,
    resolve_strategy_visualization,
)

_T0 = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
_SYMBOLS = ("600519.SH", "000858.SZ", "000001.SZ")


class _FakeDraftModel:
    def __init__(self, payload: dict) -> None:
        self._response = json.dumps(payload, ensure_ascii=False)

    def generate(self, _messages) -> str:
        return self._response


def _source() -> StrategyTemplateSource:
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH",),
            as_of=date(2025, 6, 30),
            lookback_days=(20,),
            candidate_universe="qe4-version-fixture",
            requested_outputs=("strategy", "backtest"),
        ),
        created_at=_T0,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="7" * 64,
            as_of=date(2025, 6, 30),
            start_date=date(2023, 1, 3),
            end_date=date(2025, 6, 30),
            adjustment="qfq",
            symbols=_SYMBOLS,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in _SYMBOLS},
        ),
        parent_refs=(research.ref(),),
        created_at=_T0,
    )
    return StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=_SYMBOLS,
    )


def _ready_result(
    *,
    threshold: float = 0.0,
    title: str = "动量阈值",
):
    return draft_strategy_from_language(
        _FakeDraftModel(
            {
                "schema_version": "vibe.strategy-draft.v1",
                "template_id": "factor_threshold",
                "title": title,
                "factor_field": "momentum_20d",
                "operator": "gt",
                "threshold": threshold,
            }
        ),
        StrategyDraftRequest(
            user_text=f"{title}：动量大于 {threshold}"
        ),
        source=_source(),
        created_at=_T0,
    )


def _clarification_result():
    return draft_strategy_from_language(
        _FakeDraftModel(
            {
                "schema_version": "vibe.strategy-draft.v1",
                "template_id": None,
                "ambiguities": [
                    {
                        "code": "template_choice",
                        "question": "按排名还是绝对阈值？",
                        "options": [
                            "top_n_rebalance",
                            "factor_threshold",
                        ],
                    }
                ],
            }
        ),
        StrategyDraftRequest(user_text="做一个动量策略"),
        source=_source(),
        created_at=_T0,
    )


def _create_ready(store: StrategyVersionStore, stream_id: str = "session-1"):
    return store.create_initial_version(
        stream_id=stream_id,
        owner_scope="household:v1",
        result=_ready_result(),
        created_at=_T0,
    )


def _prepare(
    store: StrategyVersionStore,
    head,
    *,
    issued_at: datetime = _T0 + timedelta(minutes=1),
    lifetime: timedelta = timedelta(minutes=10),
):
    return store.prepare_confirmation(
        stream_id=head.stream_id,
        expected_head=head,
        issued_at=issued_at,
        expires_at=issued_at + lifetime,
    )


def test_nl08_initial_ready_version_is_immutable_and_content_addressed(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, head = _create_ready(store)
        loaded = store.get_version(version.version_id)

        assert loaded == version
        assert version.version_number == 1
        assert version.parent_version_id is None
        assert version.diff == ()
        assert head.state == "draft"
        assert head.revision == 1
        assert store.list_versions("session-1") == (version,)
        assert tuple(event.state for event in store.list_events("session-1")) == (
            "draft",
        )


def test_needs_clarification_version_cannot_enter_confirmation(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, head = store.create_initial_version(
            stream_id="session-clarify",
            owner_scope="household:v1",
            result=_clarification_result(),
            created_at=_T0,
        )

        assert version.draft_status == "needs_clarification"
        assert head.state == "needs_clarification"
        with pytest.raises(
            InvalidStrategyTransitionError,
            match="cannot prepare",
        ):
            _prepare(store, head)


def test_clarification_answer_creates_ready_child_draft(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        first, head = store.create_initial_version(
            stream_id="session-clarify",
            owner_scope="household:v1",
            result=_clarification_result(),
            created_at=_T0,
        )
        second, second_head = store.modify_version(
            stream_id="session-clarify",
            expected_head=head,
            result=_ready_result(),
            created_at=_T0 + timedelta(minutes=1),
        )

        assert second.parent_version_id == first.version_id
        assert second.draft_status == "ready"
        assert second_head.state == "draft"
        assert any(
            item.path == "$.draft_status" for item in second.diff
        )


def test_confirmation_card_binds_full_spec_defaults_version_and_expiry(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, draft_head = _create_ready(store)
        card, awaiting_head = _prepare(store, draft_head)

        assert awaiting_head.state == "awaiting_confirmation"
        assert awaiting_head.version_id == version.version_id
        assert awaiting_head.event_id != draft_head.event_id
        assert card.version_id == version.version_id
        assert card.strategy == version.strategy
        assert card.strategy_spec_ref == version.strategy_spec_ref
        assert card.defaults == version.defaults
        assert store.get_confirmation_card(card.confirmation_hash) == card


def test_nl09_confirm_creates_receipt_and_append_only_confirmed_event(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        receipt = store.confirm(
            stream_id="session-1",
            expected_head=awaiting,
            confirmation_hash=card.confirmation_hash,
            idempotency_key="confirm-click-1",
            actor_id="user-1",
            confirmed_at=_T0 + timedelta(minutes=2),
        )

        confirmed = store.get_head("session-1")
        assert confirmed.state == "confirmed"
        assert confirmed.version_id == version.version_id
        assert receipt.version_id == version.version_id
        assert receipt.confirmation_hash == card.confirmation_hash
        assert store.get_confirmation_receipt(receipt.receipt_id) == receipt
        assert tuple(event.state for event in store.list_events("session-1")) == (
            "draft",
            "awaiting_confirmation",
            "confirmed",
        )


def test_visualization_resolution_binds_current_card_hash_and_canonical_receipt(
    tmp_path: Path,
) -> None:
    source = _source()
    snapshot = DataSnapshotRef.model_validate(source.snapshot.payload)
    data_basis = StrategyDataBasis.from_snapshot(source.snapshot.ref(), snapshot)
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        version, initial = _create_ready(store)
        first_card, first_head = _prepare(
            store,
            initial,
            lifetime=timedelta(minutes=1),
        )
        _, first_payload = build_strategy_visualization(
            version=version,
            head=first_head,
            card=first_card,
            data_basis=data_basis,
            now=_T0 + timedelta(minutes=1),
        )
        second_card, second_head = store.prepare_confirmation(
            stream_id=initial.stream_id,
            expected_head=first_head,
            issued_at=first_card.expires_at,
            expires_at=first_card.expires_at + timedelta(minutes=10),
        )
        _, second_payload = build_strategy_visualization(
            version=version,
            head=second_head,
            card=second_card,
            data_basis=data_basis,
            now=first_card.expires_at,
        )
        receipt = store.confirm(
            stream_id=initial.stream_id,
            expected_head=second_head,
            confirmation_hash=second_card.confirmation_hash,
            idempotency_key="confirm-renewed-card",
            actor_id="household-user",
            confirmed_at=first_card.expires_at + timedelta(minutes=1),
        )

        old_resolved = resolve_strategy_visualization(
            first_payload,
            store,
            now=first_card.expires_at + timedelta(minutes=1),
        )
        current_resolved = resolve_strategy_visualization(
            second_payload,
            store,
            now=first_card.expires_at + timedelta(minutes=1),
        )

        assert old_resolved.lifecycle_state == "superseded"
        assert old_resolved.receipt is None
        assert current_resolved.lifecycle_state == "confirmed"
        assert current_resolved.receipt == receipt


def test_strategy_version_store_rejects_symlinks_and_secures_database_files(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "versions.db"
    with StrategyVersionStore(db_path) as store:
        _create_ready(store)
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        for sidecar in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    outside = tmp_path / "outside.db"
    outside.touch()
    linked = tmp_path / "linked.db"
    linked.symlink_to(outside)
    with pytest.raises(StrategyVersionStoreIntegrityError, match="symlink"):
        StrategyVersionStore(linked)


def test_nl09_duplicate_confirmation_key_returns_same_receipt_once(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        _, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        kwargs = {
            "stream_id": "session-1",
            "expected_head": awaiting,
            "confirmation_hash": card.confirmation_hash,
            "idempotency_key": "double-click",
            "actor_id": "user-1",
        }
        first = store.confirm(
            **kwargs,
            confirmed_at=_T0 + timedelta(minutes=2),
        )
        retry = store.confirm(
            **kwargs,
            confirmed_at=_T0 + timedelta(minutes=3),
        )

        assert retry == first
        assert len(store.list_events("session-1")) == 3


def test_nl09_idempotency_key_reuse_with_other_semantics_fails(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        _, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        store.confirm(
            stream_id="session-1",
            expected_head=awaiting,
            confirmation_hash=card.confirmation_hash,
            idempotency_key="same-key",
            actor_id="user-1",
            confirmed_at=_T0 + timedelta(minutes=2),
        )

        with pytest.raises(StrategyIdempotencyConflict):
            store.confirm(
                stream_id="session-1",
                expected_head=awaiting,
                confirmation_hash="a" * 64,
                idempotency_key="same-key",
                actor_id="user-1",
                confirmed_at=_T0 + timedelta(minutes=3),
            )


def test_nl09_wrong_or_expired_confirmation_card_fails_closed(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "wrong.db") as store:
        _, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        with pytest.raises(StrategyConfirmationHashMismatch):
            store.confirm(
                stream_id="session-1",
                expected_head=awaiting,
                confirmation_hash="b" * 64,
                idempotency_key="wrong-card",
                actor_id="user-1",
                confirmed_at=_T0 + timedelta(minutes=2),
            )

    with StrategyVersionStore(tmp_path / "expired.db") as store:
        _, head = _create_ready(store)
        card, awaiting = _prepare(
            store,
            head,
            lifetime=timedelta(minutes=1),
        )
        with pytest.raises(ExpiredStrategyConfirmationError):
            store.confirm(
                stream_id="session-1",
                expected_head=awaiting,
                confirmation_hash=card.confirmation_hash,
                idempotency_key="expired-card",
                actor_id="user-1",
                confirmed_at=card.expires_at,
            )


def test_expired_card_can_be_reissued_but_unexpired_card_cannot(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        _, head = _create_ready(store)
        first_card, awaiting = _prepare(
            store,
            head,
            lifetime=timedelta(minutes=1),
        )
        with pytest.raises(
            InvalidStrategyTransitionError,
            match="has not expired",
        ):
            store.prepare_confirmation(
                stream_id="session-1",
                expected_head=awaiting,
                issued_at=first_card.issued_at + timedelta(seconds=30),
                expires_at=first_card.expires_at + timedelta(minutes=10),
            )

        second_card, renewed = store.prepare_confirmation(
            stream_id="session-1",
            expected_head=awaiting,
            issued_at=first_card.expires_at,
            expires_at=first_card.expires_at + timedelta(minutes=10),
        )

        assert second_card.confirmation_hash != first_card.confirmation_hash
        assert renewed.state == "awaiting_confirmation"
        assert renewed.event_id != awaiting.event_id
        with pytest.raises(StaleStrategyHeadError):
            store.confirm(
                stream_id="session-1",
                expected_head=awaiting,
                confirmation_hash=first_card.confirmation_hash,
                idempotency_key="old-expired-card",
                actor_id="user-1",
                confirmed_at=first_card.expires_at + timedelta(minutes=1),
            )


def test_nl08_modification_creates_child_diff_and_preserves_old_version(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        first, head = _create_ready(store)
        second, second_head = store.modify_version(
            stream_id="session-1",
            expected_head=head,
            result=_ready_result(threshold=0.1, title="动量阈值 10%"),
            created_at=_T0 + timedelta(minutes=1),
        )

        assert second.version_number == 2
        assert second.parent_version_id == first.version_id
        assert second_head.state == "draft"
        assert store.get_version(first.version_id) == first
        assert store.list_versions("session-1") == (first, second)
        paths = {item.path for item in second.diff}
        assert "$.strategy.signals[0].value" in paths
        assert "$.strategy.title" in paths
        assert "$.proposal.threshold" in paths
        card, _ = _prepare(store, second_head)
        assert card.version_number == 2
        assert card.parent_version_id == first.version_id
        assert card.diff == second.diff


def test_old_confirmation_card_is_stale_after_modification(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        _, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        _, new_head = store.modify_version(
            stream_id="session-1",
            expected_head=awaiting,
            result=_ready_result(threshold=0.2),
            created_at=_T0 + timedelta(minutes=2),
        )

        assert new_head.version_id != awaiting.version_id
        with pytest.raises(StaleStrategyHeadError):
            store.confirm(
                stream_id="session-1",
                expected_head=awaiting,
                confirmation_hash=card.confirmation_hash,
                idempotency_key="stale-card",
                actor_id="user-1",
                confirmed_at=_T0 + timedelta(minutes=3),
            )


def test_confirmed_version_can_be_parent_of_new_traceable_draft(
    tmp_path: Path,
) -> None:
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        first, head = _create_ready(store)
        card, awaiting = _prepare(store, head)
        store.confirm(
            stream_id="session-1",
            expected_head=awaiting,
            confirmation_hash=card.confirmation_hash,
            idempotency_key="confirm-v1",
            actor_id="user-1",
            confirmed_at=_T0 + timedelta(minutes=2),
        )
        confirmed_head = store.get_head("session-1")
        second, second_head = store.modify_version(
            stream_id="session-1",
            expected_head=confirmed_head,
            result=_ready_result(threshold=0.3),
            created_at=_T0 + timedelta(minutes=3),
        )

        assert second.parent_version_id == first.version_id
        assert second_head.state == "draft"
        assert store.get_version(first.version_id) == first


def test_nl09_confirm_modify_race_allows_exactly_one_cas_winner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "race.db"
    first_store = StrategyVersionStore(db_path)
    second_store = StrategyVersionStore(db_path)
    try:
        _, head = _create_ready(first_store, stream_id="race-session")
        card, awaiting = _prepare(first_store, head)
        barrier = Barrier(2)

        def confirm():
            barrier.wait()
            return first_store.confirm(
                stream_id="race-session",
                expected_head=awaiting,
                confirmation_hash=card.confirmation_hash,
                idempotency_key="race-confirm",
                actor_id="user-1",
                confirmed_at=_T0 + timedelta(minutes=2),
            )

        def modify():
            barrier.wait()
            return second_store.modify_version(
                stream_id="race-session",
                expected_head=awaiting,
                result=_ready_result(threshold=0.4),
                created_at=_T0 + timedelta(minutes=2),
            )

        outcomes: list[object] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm), pool.submit(modify)]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(exc)

        assert sum(
            not isinstance(item, Exception) for item in outcomes
        ) == 1
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], StaleStrategyHeadError)
        final_head = first_store.get_head("race-session")
        assert final_head.state in {"confirmed", "draft"}
    finally:
        first_store.close()
        second_store.close()


def test_rejected_draft_cannot_create_version(tmp_path: Path) -> None:
    rejected = draft_strategy_from_language(
        _FakeDraftModel(
            {
                "schema_version": "vibe.strategy-draft.v1",
                "template_id": "factor_threshold",
                "factor_field": "python_eval",
                "operator": "gt",
                "threshold": 0,
            }
        ),
        StrategyDraftRequest(user_text="运行 Python"),
        source=_source(),
    )
    with StrategyVersionStore(tmp_path / "versions.db") as store:
        with pytest.raises(StrategyVersionError, match="rejected"):
            store.create_initial_version(
                stream_id="rejected-session",
                owner_scope="household:v1",
                result=rejected,
            )


def test_persisted_version_tampering_fails_integrity_validation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "versions.db"
    with StrategyVersionStore(db_path) as store:
        version, _ = _create_ready(store)

    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT payload_json FROM strategy_versions WHERE version_id=?",
        (version.version_id,),
    ).fetchone()
    payload = json.loads(row[0])
    payload["strategy"]["title"] = "tampered"
    connection.execute(
        "UPDATE strategy_versions SET payload_json=? WHERE version_id=?",
        (json.dumps(payload), version.version_id),
    )
    connection.commit()
    connection.close()

    with StrategyVersionStore(db_path) as store:
        with pytest.raises(StrategyVersionStoreIntegrityError):
            store.get_version(version.version_id)


def test_persisted_head_event_column_tampering_fails_integrity_validation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "versions.db"
    with StrategyVersionStore(db_path) as store:
        _, head = _create_ready(store)

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE strategy_state_events SET state='confirmed' WHERE event_id=?",
        (head.event_id,),
    )
    connection.commit()
    connection.close()

    with StrategyVersionStore(db_path) as store:
        with pytest.raises(StrategyVersionStoreIntegrityError):
            store.get_head("session-1")
