"""Atomic, content-addressed persistence for QE1 research objects."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from .contracts import (
    DEFAULT_OWNER_SCOPE,
    ObjectRef,
    ObjectType,
    ResearchObject,
    canonical_json,
)

DEFAULT_TOTAL_QUOTA_BYTES = 1_073_741_824
DEFAULT_MAX_OBJECT_BYTES = 67_108_864


class ResearchStoreError(RuntimeError):
    """Base class for fail-closed research-store errors."""


class StoreIntegrityError(ResearchStoreError):
    """Raised when persisted content or the store layout is inconsistent."""


class QuotaExceededError(ResearchStoreError):
    """Raised before a write would exceed an object or household quota."""


@dataclass(frozen=True)
class PutResult:
    """Result of an idempotent store write."""

    object: ResearchObject
    created: bool


class ResearchStore:
    """Immutable JSON object store with a rebuildable SQLite/WAL index.

    JSON objects are the source of truth.  The SQLite database is an index and
    can be rebuilt exclusively from validated object files.  Every mutating
    operation takes a process-wide flock in addition to SQLite's transaction,
    which makes quota checks and atomic rename safe across worker processes.
    """

    def __init__(
        self,
        root: Path,
        *,
        total_quota_bytes: int = DEFAULT_TOTAL_QUOTA_BYTES,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
    ) -> None:
        if total_quota_bytes <= 0 or max_object_bytes <= 0:
            raise ValueError("store quotas must be positive")
        if max_object_bytes > total_quota_bytes:
            raise ValueError("max_object_bytes cannot exceed total_quota_bytes")
        self.root = Path(root)
        self.total_quota_bytes = total_quota_bytes
        self.max_object_bytes = max_object_bytes
        self.objects_dir = self.root / "objects"
        self.database_path = self.root / "research.db"
        self.lock_path = self.root / ".store.lock"
        self._prepare_root()
        self._initialize_database()

    @classmethod
    def default(cls) -> "ResearchStore":
        """Open the household store in the existing backed-up vibe-home root."""

        return cls(Path.home() / ".vibe-trading" / "research")

    def put(self, research_object: ResearchObject) -> PutResult:
        """Persist one immutable object or return the existing identical object."""

        encoded = (canonical_json(research_object) + "\n").encode("utf-8")
        if len(encoded) > self.max_object_bytes:
            raise QuotaExceededError(
                f"object is {len(encoded)} bytes; limit is {self.max_object_bytes}"
            )
        target = self._object_path(research_object.object_type, research_object.content_sha256)
        with self._exclusive_lock():
            self._validate_parent_refs(research_object)
            if target.exists() or target.is_symlink():
                existing = self._load_path(target)
                if existing.object_id != research_object.object_id:
                    raise StoreIntegrityError("existing object path has a different object identity")
                self._index_object(existing, target)
                return PutResult(object=existing, created=False)
            usage = self._object_bytes_on_disk()
            if usage + len(encoded) > self.total_quota_bytes:
                raise QuotaExceededError(
                    f"write would exceed household quota: {usage} + {len(encoded)} "
                    f"> {self.total_quota_bytes}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, encoded)
            # A crash here leaves a valid source-of-truth JSON object.  A retry
            # or rebuild_index() restores the disposable SQLite row.
            self._index_object(research_object, target)
            return PutResult(object=research_object, created=True)

    def get(
        self,
        object_id: str,
        *,
        owner_scope: str = DEFAULT_OWNER_SCOPE,
    ) -> ResearchObject | None:
        """Return a validated object only when it belongs to ``owner_scope``."""

        object_type, digest = self._parse_object_id(object_id)
        target = self._object_path(object_type, digest)
        if not target.exists() and not target.is_symlink():
            return None
        loaded = self._load_path(target)
        if loaded.object_id != object_id:
            raise StoreIntegrityError("object file identity does not match its path")
        if loaded.owner_scope != owner_scope:
            return None
        self._index_object(loaded, target)
        return loaded

    def list_refs(
        self,
        *,
        owner_scope: str = DEFAULT_OWNER_SCOPE,
        object_type: ObjectType | None = None,
        limit: int = 100,
    ) -> tuple[ObjectRef, ...]:
        """List newest indexed object references within one owner scope."""

        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        query = (
            "SELECT object_type, object_id, content_sha256 FROM objects "
            "WHERE owner_scope = ?"
        )
        params: list[object] = [owner_scope]
        if object_type is not None:
            query += " AND object_type = ?"
            params.append(object_type)
        query += " ORDER BY created_at DESC, object_id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            ObjectRef(object_type=row[0], object_id=row[1], content_sha256=row[2])
            for row in rows
        )

    def quota_usage_bytes(self) -> int:
        """Return validated regular-file bytes currently charged to the quota."""

        with self._exclusive_lock():
            return self._object_bytes_on_disk()

    def rebuild_index(self) -> int:
        """Validate every object file, then replace the SQLite index atomically."""

        with self._exclusive_lock():
            objects: list[tuple[ResearchObject, Path]] = []
            if self.objects_dir.exists():
                for path in self._iter_object_paths():
                    loaded = self._load_path(path)
                    expected = self._object_path(loaded.object_type, loaded.content_sha256)
                    if path != expected:
                        raise StoreIntegrityError(f"object is stored at a non-canonical path: {path}")
                    objects.append((loaded, path))
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM parents")
                connection.execute("DELETE FROM objects")
                for loaded, path in objects:
                    self._insert_index_rows(connection, loaded, path)
                connection.commit()
            return len(objects)

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise StoreIntegrityError("research store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.objects_dir.is_symlink():
            raise StoreIntegrityError("research objects directory must not be a symlink")
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path.is_symlink():
            raise StoreIntegrityError("research database must not be a symlink")
        if self.lock_path.is_symlink():
            raise StoreIntegrityError("research store lock must not be a symlink")

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    object_id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_objects_owner_created
                    ON objects(owner_scope, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_objects_owner_type_created
                    ON objects(owner_scope, object_type, created_at DESC);
                CREATE TABLE IF NOT EXISTS parents (
                    object_id TEXT NOT NULL,
                    parent_object_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY(object_id, parent_object_id)
                );
                CREATE INDEX IF NOT EXISTS idx_parents_parent
                    ON parents(parent_object_id);
                """
            )
            connection.commit()

    def _index_object(self, research_object: ResearchObject, path: Path) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_index_rows(connection, research_object, path)
            connection.commit()

    def _insert_index_rows(
        self,
        connection: sqlite3.Connection,
        research_object: ResearchObject,
        path: Path,
    ) -> None:
        relative_path = path.relative_to(self.root).as_posix()
        size_bytes = path.stat(follow_symlinks=False).st_size
        connection.execute(
            """
            INSERT INTO objects(
                object_id, owner_scope, object_type, content_sha256,
                created_at, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_id) DO UPDATE SET
                owner_scope = excluded.owner_scope,
                object_type = excluded.object_type,
                content_sha256 = excluded.content_sha256,
                created_at = excluded.created_at,
                relative_path = excluded.relative_path,
                size_bytes = excluded.size_bytes
            """,
            (
                research_object.object_id,
                research_object.owner_scope,
                research_object.object_type,
                research_object.content_sha256,
                research_object.created_at.isoformat(),
                relative_path,
                size_bytes,
            ),
        )
        connection.execute("DELETE FROM parents WHERE object_id = ?", (research_object.object_id,))
        connection.executemany(
            "INSERT INTO parents(object_id, parent_object_id, position) VALUES (?, ?, ?)",
            [
                (research_object.object_id, parent.object_id, position)
                for position, parent in enumerate(research_object.parent_refs)
            ],
        )

    def _validate_parent_refs(self, research_object: ResearchObject) -> None:
        for parent in research_object.parent_refs:
            path = self._object_path(parent.object_type, parent.content_sha256)
            if not path.exists() and not path.is_symlink():
                raise StoreIntegrityError(f"parent object is not stored: {parent.object_id}")
            loaded = self._load_path(path)
            if loaded.ref() != parent:
                raise StoreIntegrityError(f"parent object content does not match: {parent.object_id}")
            if loaded.owner_scope != research_object.owner_scope:
                raise StoreIntegrityError("parent object belongs to a different owner_scope")

    def _load_path(self, path: Path) -> ResearchObject:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise StoreIntegrityError(f"object file disappeared: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise StoreIntegrityError(f"object path is not a regular file: {path}")
        if metadata.st_size > self.max_object_bytes:
            raise StoreIntegrityError(f"object file exceeds size limit: {path}")
        try:
            raw = path.read_bytes()
            return ResearchObject.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise StoreIntegrityError(f"invalid research object {path}: {exc}") from exc

    def _object_bytes_on_disk(self) -> int:
        total = 0
        if not self.objects_dir.exists():
            return total
        for path in self._iter_object_paths():
            metadata = path.lstat()
            total += metadata.st_size
        return total

    def _iter_object_paths(self) -> tuple[Path, ...]:
        allowed_types = {
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
        paths: list[Path] = []
        for type_dir in sorted(self.objects_dir.iterdir()):
            metadata = type_dir.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or type_dir.name not in allowed_types:
                raise StoreIntegrityError(f"invalid object type directory: {type_dir}")
            for path in sorted(type_dir.iterdir()):
                if path.name.startswith(".") and path.suffix == ".tmp":
                    continue
                file_metadata = path.lstat()
                if path.suffix != ".json" or not stat.S_ISREG(file_metadata.st_mode):
                    raise StoreIntegrityError(f"non-regular object entry: {path}")
                paths.append(path)
        return tuple(paths)

    def _object_path(self, object_type: ObjectType, digest: str) -> Path:
        if not _is_sha256(digest):
            raise StoreIntegrityError("invalid object digest")
        type_dir = self.objects_dir / object_type
        if type_dir.is_symlink():
            raise StoreIntegrityError("object type directory must not be a symlink")
        return type_dir / f"{digest}.json"

    @staticmethod
    def _parse_object_id(object_id: str) -> tuple[ObjectType, str]:
        try:
            raw_type, digest = object_id.split(":", 1)
        except ValueError as exc:
            raise StoreIntegrityError("invalid object ID") from exc
        allowed: tuple[ObjectType, ...] = (
            "research_spec",
            "data_snapshot_ref",
            "peer_set",
            "factor_evidence",
            "similarity_run",
            "strategy_spec",
            "engine_request",
            "backtest_run",
            "research_report",
        )
        if raw_type not in allowed or not _is_sha256(digest):
            raise StoreIntegrityError("invalid object ID")
        return raw_type, digest  # type: ignore[return-value]

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.stem}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
