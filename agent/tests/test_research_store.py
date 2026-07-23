"""QE1 WK-03/PS-01/PS-03 tests for the content-addressed research store."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.research.contracts import (
    DataSnapshotRef,
    ResearchSpec,
    canonical_json,
    create_research_object,
)
from src.research.store import (
    QuotaExceededError,
    ResearchStore,
    ResearchStoreError,
    StoreIntegrityError,
)


def _research_object(
    *,
    symbol: str = "600519.SH",
    owner_scope: str = "household:v1",
    created_at: datetime | None = None,
):
    return create_research_object(
        ResearchSpec(
            symbols=(symbol,),
            as_of=date(2025, 6, 30),
            lookback_days=(120,),
            candidate_universe="csi300@2025-06-30",
        ),
        owner_scope=owner_scope,
        created_at=created_at or datetime(2026, 7, 23, tzinfo=timezone.utc),
    )


def _snapshot_object(parent, *, owner_scope: str = "household:v1"):
    return create_research_object(
        DataSnapshotRef(
            snapshot_sha256="1" * 64,
            as_of=date(2025, 6, 30),
            start_date=date(2025, 1, 2),
            end_date=date(2025, 6, 30),
            adjustment="qfq",
            symbols=("600519.SH",),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={"600519.SH": "fixture"},
        ),
        owner_scope=owner_scope,
        parent_refs=(parent.ref(),),
        created_at=datetime(2026, 7, 23, 0, 1, tzinfo=timezone.utc),
    )


def test_wk03_repeated_semantic_write_is_idempotent(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    first = _research_object(created_at=datetime(2026, 7, 23, tzinfo=timezone.utc))
    retry = _research_object(created_at=datetime(2026, 7, 24, tzinfo=timezone.utc))

    initial = store.put(first)
    path = store.objects_dir / "research_spec" / f"{first.content_sha256}.json"
    initial_mtime = path.stat().st_mtime_ns
    repeated = store.put(retry)

    assert initial.created is True
    assert repeated.created is False
    assert repeated.object == first
    assert path.stat().st_mtime_ns == initial_mtime
    assert store.get(first.object_id) == first
    assert store.list_refs() == (first.ref(),)


def test_wk03_concurrent_retries_create_one_object(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    research_object = _research_object()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: store.put(research_object), range(16)))

    assert sum(result.created for result in results) == 1
    assert {result.object.object_id for result in results} == {research_object.object_id}
    assert store.list_refs() == (research_object.ref(),)


def test_ps01_parent_must_exist_and_match_owner_scope(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    parent = _research_object()
    child = _snapshot_object(parent)

    with pytest.raises(StoreIntegrityError, match="parent object is not stored"):
        store.put(child)

    store.put(parent)
    assert store.put(child).created is True

    other_parent = _research_object(owner_scope="household:other")
    store.put(other_parent)
    cross_owner_child = _snapshot_object(other_parent, owner_scope="household:v1")
    with pytest.raises(StoreIntegrityError, match="different owner_scope"):
        store.put(cross_owner_child)


def test_ps01_index_failure_leaves_recoverable_complete_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "research"
    store = ResearchStore(root)
    research_object = _research_object()

    def fail_index(*_args, **_kwargs):
        raise ResearchStoreError("simulated index crash")

    monkeypatch.setattr(store, "_index_object", fail_index)
    with pytest.raises(ResearchStoreError, match="simulated index crash"):
        store.put(research_object)

    object_path = root / "objects" / "research_spec" / f"{research_object.content_sha256}.json"
    assert object_path.is_file()
    assert not tuple(object_path.parent.glob("*.tmp"))

    recovered = ResearchStore(root)
    assert recovered.get(research_object.object_id) == research_object
    assert recovered.list_refs() == (research_object.ref(),)


def test_ps01_tampered_object_fails_closed_even_when_indexed(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    research_object = _research_object()
    store.put(research_object)
    object_path = (
        store.objects_dir / "research_spec" / f"{research_object.content_sha256}.json"
    )
    raw = json.loads(object_path.read_text(encoding="utf-8"))
    raw["payload"]["candidate_universe"] = "tampered"
    object_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(StoreIntegrityError, match="invalid research object"):
        store.get(research_object.object_id)
    with pytest.raises(StoreIntegrityError, match="invalid research object"):
        store.rebuild_index()


def test_ps01_rebuild_index_restores_rows_from_json_source_of_truth(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    first = _research_object(symbol="600519.SH")
    second = _research_object(symbol="000858.SZ")
    store.put(first)
    store.put(second)

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM parents")
        connection.execute("DELETE FROM objects")
        connection.commit()
    assert store.list_refs() == ()

    assert store.rebuild_index() == 2
    assert {ref.object_id for ref in store.list_refs()} == {first.object_id, second.object_id}


def test_ps03_owner_scope_is_not_inferred_from_a_secret(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    household = _research_object(owner_scope="household:v1")
    other = _research_object(owner_scope="household:other")
    store.put(household)
    store.put(other)

    assert store.get(household.object_id, owner_scope="household:v1") == household
    assert store.get(household.object_id, owner_scope="household:other") is None
    assert store.list_refs(owner_scope="household:v1") == (household.ref(),)
    assert store.list_refs(owner_scope="household:other") == (other.ref(),)


def test_ps03_object_size_limit_fails_before_any_file_is_written(tmp_path: Path) -> None:
    store = ResearchStore(
        tmp_path / "research",
        total_quota_bytes=1_024,
        max_object_bytes=128,
    )
    research_object = _research_object()
    assert len((canonical_json(research_object) + "\n").encode("utf-8")) > 128

    with pytest.raises(QuotaExceededError, match="object is"):
        store.put(research_object)
    assert not tuple(store.objects_dir.glob("*/*.json"))


def test_ps01_symlink_object_type_directory_is_rejected(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.objects_dir / "research_spec").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoreIntegrityError, match="must not be a symlink"):
        store.put(_research_object())
    assert not tuple(outside.iterdir())


@pytest.mark.parametrize("entry_name", ["research.db", ".store.lock"])
def test_ps01_symlink_database_or_lock_is_rejected(tmp_path: Path, entry_name: str) -> None:
    root = tmp_path / "research"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not touch", encoding="utf-8")
    (root / entry_name).symlink_to(outside)

    with pytest.raises(StoreIntegrityError, match="must not be a symlink"):
        ResearchStore(root)
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_ps03_total_quota_rejects_new_write_without_deleting_history(tmp_path: Path) -> None:
    first = _research_object(symbol="600519.SH")
    second = _research_object(symbol="000858.SZ")
    first_size = len((canonical_json(first) + "\n").encode("utf-8"))
    second_size = len((canonical_json(second) + "\n").encode("utf-8"))
    quota = first_size + second_size - 1
    store = ResearchStore(
        tmp_path / "research",
        total_quota_bytes=quota,
        max_object_bytes=max(first_size, second_size),
    )

    store.put(first)
    with pytest.raises(QuotaExceededError, match="household quota"):
        store.put(second)

    assert store.get(first.object_id) == first
    assert store.get(second.object_id) is None
    assert store.quota_usage_bytes() == first_size
