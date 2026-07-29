"""SQLite append-only store for QE4 immutable strategy versions and state."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .drafting import StrategyDraftResult
from .versioning import (
    ExpiredStrategyConfirmationError,
    InvalidStrategyTransitionError,
    StaleStrategyHeadError,
    StrategyConfirmationCard,
    StrategyConfirmationHashMismatch,
    StrategyConfirmationReceipt,
    StrategyHeadToken,
    StrategyIdempotencyConflict,
    StrategyStateEvent,
    StrategyVersion,
    StrategyVersionError,
    StrategyVersionSource,
    create_strategy_confirmation_card,
    create_strategy_confirmation_receipt,
    create_strategy_state_event,
    create_strategy_version,
    initial_state_for_version,
)


class StrategyVersionStoreIntegrityError(RuntimeError):
    """Raised when persisted version-state data fails strict validation."""


class StrategyVersionStore:
    """Cross-process-safe SQLite store with compare-and-swap head tokens."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    version_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    parent_version_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(stream_id, version_number),
                    FOREIGN KEY(parent_version_id)
                        REFERENCES strategy_versions(version_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_state_events (
                    event_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    version_id TEXT NOT NULL,
                    prior_event_id TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'draft', 'needs_clarification',
                        'awaiting_confirmation', 'confirmed'
                    )),
                    confirmation_hash TEXT,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(stream_id, sequence),
                    FOREIGN KEY(version_id)
                        REFERENCES strategy_versions(version_id),
                    FOREIGN KEY(prior_event_id)
                        REFERENCES strategy_state_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_heads (
                    stream_id TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    FOREIGN KEY(version_id)
                        REFERENCES strategy_versions(version_id),
                    FOREIGN KEY(event_id)
                        REFERENCES strategy_state_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_confirmation_cards (
                    confirmation_hash TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(version_id)
                        REFERENCES strategy_versions(version_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_confirmation_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    confirmation_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    UNIQUE(stream_id, idempotency_key),
                    UNIQUE(version_id, confirmation_hash),
                    FOREIGN KEY(version_id)
                        REFERENCES strategy_versions(version_id),
                    FOREIGN KEY(confirmation_hash)
                        REFERENCES strategy_confirmation_cards(confirmation_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_strategy_versions_stream
                    ON strategy_versions(stream_id, version_number);
                CREATE INDEX IF NOT EXISTS idx_strategy_events_stream
                    ON strategy_state_events(stream_id, sequence);
                """
            )
            self._conn.commit()

    @contextmanager
    def _write_transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @staticmethod
    def _json(value) -> str:
        return value.model_dump_json()

    @staticmethod
    def _parse(model_type, raw: str):
        try:
            return model_type.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise StrategyVersionStoreIntegrityError(
                f"persisted {model_type.__name__} failed validation"
            ) from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "StrategyVersionStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _get_version_locked(self, version_id: str) -> StrategyVersion | None:
        row = self._conn.execute(
            """
            SELECT version_id, stream_id, version_number, parent_version_id,
                   payload_json
            FROM strategy_versions
            WHERE version_id=?
            """,
            (version_id,),
        ).fetchone()
        if row is None:
            return None
        version = self._parse(StrategyVersion, row["payload_json"])
        if (
            version.version_id != row["version_id"]
            or version.stream_id != row["stream_id"]
            or version.version_number != row["version_number"]
            or version.parent_version_id != row["parent_version_id"]
        ):
            raise StrategyVersionStoreIntegrityError(
                "strategy version index columns do not match payload"
            )
        return version

    def get_version(self, version_id: str) -> StrategyVersion | None:
        with self._lock:
            return self._get_version_locked(version_id)

    def list_versions(self, stream_id: str) -> tuple[StrategyVersion, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT version_id, payload_json
                FROM strategy_versions
                WHERE stream_id=?
                ORDER BY version_number ASC
                """,
                (stream_id,),
            ).fetchall()
            versions = tuple(
                self._parse(StrategyVersion, row["payload_json"])
                for row in rows
            )
            if any(
                version.version_id != row["version_id"]
                or version.stream_id != stream_id
                for version, row in zip(versions, rows)
            ):
                raise StrategyVersionStoreIntegrityError(
                    "strategy version listing index mismatch"
                )
            for index, version in enumerate(versions):
                expected_number = index + 1
                expected_parent = (
                    versions[index - 1].version_id if index > 0 else None
                )
                if (
                    version.version_number != expected_number
                    or version.parent_version_id != expected_parent
                ):
                    raise StrategyVersionStoreIntegrityError(
                        "strategy version chain is not contiguous"
                    )
            return versions

    def list_events(self, stream_id: str) -> tuple[StrategyStateEvent, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_id, payload_json
                FROM strategy_state_events
                WHERE stream_id=?
                ORDER BY sequence ASC
                """,
                (stream_id,),
            ).fetchall()
            events = tuple(
                self._parse(StrategyStateEvent, row["payload_json"])
                for row in rows
            )
            if any(
                event.event_id != row["event_id"]
                or event.stream_id != stream_id
                for event, row in zip(events, rows)
            ):
                raise StrategyVersionStoreIntegrityError(
                    "strategy event listing index mismatch"
                )
            for index, event in enumerate(events):
                expected_sequence = index + 1
                expected_prior = (
                    events[index - 1].event_id if index > 0 else None
                )
                if (
                    event.sequence != expected_sequence
                    or event.prior_event_id != expected_prior
                ):
                    raise StrategyVersionStoreIntegrityError(
                        "strategy event chain is not contiguous"
                    )
            return events

    def _get_event_locked(
        self,
        event_id: str,
    ) -> StrategyStateEvent | None:
        row = self._conn.execute(
            """
            SELECT event_id, stream_id, sequence, version_id, prior_event_id,
                   state, confirmation_hash, payload_json
            FROM strategy_state_events
            WHERE event_id=?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        event = self._parse(StrategyStateEvent, row["payload_json"])
        if (
            event.event_id != row["event_id"]
            or event.stream_id != row["stream_id"]
            or event.sequence != row["sequence"]
            or event.version_id != row["version_id"]
            or event.prior_event_id != row["prior_event_id"]
            or event.state != row["state"]
            or event.confirmation_hash != row["confirmation_hash"]
        ):
            raise StrategyVersionStoreIntegrityError(
                "strategy event index columns do not match payload"
            )
        return event

    def _get_head_locked(self, stream_id: str) -> StrategyHeadToken | None:
        row = self._conn.execute(
            """
            SELECT h.stream_id, h.version_id, h.event_id, h.revision
            FROM strategy_heads AS h
            WHERE h.stream_id=?
            """,
            (stream_id,),
        ).fetchone()
        if row is None:
            return None
        event = self._get_event_locked(row["event_id"])
        if event is None:
            raise StrategyVersionStoreIntegrityError(
                "strategy head references a missing event"
            )
        if (
            event.stream_id != row["stream_id"]
            or event.version_id != row["version_id"]
            or event.event_id != row["event_id"]
            or event.sequence != row["revision"]
        ):
            raise StrategyVersionStoreIntegrityError(
                "strategy head does not match current event"
            )
        return StrategyHeadToken(
            stream_id=event.stream_id,
            version_id=event.version_id,
            event_id=event.event_id,
            revision=event.sequence,
            state=event.state,
        )

    def get_head(self, stream_id: str) -> StrategyHeadToken | None:
        with self._lock:
            return self._get_head_locked(stream_id)

    @staticmethod
    def _require_expected_head(
        actual: StrategyHeadToken | None,
        expected: StrategyHeadToken,
    ) -> StrategyHeadToken:
        if actual is None or actual != expected:
            raise StaleStrategyHeadError(
                "strategy head changed; refresh before writing"
            )
        return actual

    def _insert_version(self, version: StrategyVersion) -> None:
        self._conn.execute(
            """
            INSERT INTO strategy_versions (
                version_id, stream_id, version_number, parent_version_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                version.version_id,
                version.stream_id,
                version.version_number,
                version.parent_version_id,
                self._json(version),
                version.created_at.isoformat(),
            ),
        )

    def _insert_event(self, event: StrategyStateEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO strategy_state_events (
                event_id, stream_id, sequence, version_id, prior_event_id,
                state, confirmation_hash, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.stream_id,
                event.sequence,
                event.version_id,
                event.prior_event_id,
                event.state,
                event.confirmation_hash,
                self._json(event),
                event.occurred_at.isoformat(),
            ),
        )

    def _replace_head(self, event: StrategyStateEvent) -> StrategyHeadToken:
        self._conn.execute(
            """
            INSERT INTO strategy_heads (
                stream_id, version_id, event_id, revision
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET
                version_id=excluded.version_id,
                event_id=excluded.event_id,
                revision=excluded.revision
            """,
            (
                event.stream_id,
                event.version_id,
                event.event_id,
                event.sequence,
            ),
        )
        return StrategyHeadToken(
            stream_id=event.stream_id,
            version_id=event.version_id,
            event_id=event.event_id,
            revision=event.sequence,
            state=event.state,
        )

    def create_initial_version(
        self,
        *,
        stream_id: str,
        owner_scope: str,
        result: StrategyDraftResult,
        source_context: StrategyVersionSource | None = None,
        created_at: datetime | None = None,
    ) -> tuple[StrategyVersion, StrategyHeadToken]:
        version = create_strategy_version(
            stream_id=stream_id,
            owner_scope=owner_scope,
            version_number=1,
            result=result,
            source_context=source_context,
            created_at=created_at,
        )
        event = create_strategy_state_event(
            stream_id=stream_id,
            sequence=1,
            version_id=version.version_id,
            state=initial_state_for_version(version),
            prior_event_id=None,
            occurred_at=created_at,
        )
        with self._lock, self._write_transaction():
            if self._get_head_locked(stream_id) is not None:
                raise StrategyVersionError(
                    "strategy stream already has an initial version"
                )
            self._insert_version(version)
            self._insert_event(event)
            head = self._replace_head(event)
        return version, head

    def modify_version(
        self,
        *,
        stream_id: str,
        expected_head: StrategyHeadToken,
        result: StrategyDraftResult,
        source_context: StrategyVersionSource | None = None,
        created_at: datetime | None = None,
    ) -> tuple[StrategyVersion, StrategyHeadToken]:
        with self._lock, self._write_transaction():
            actual = self._require_expected_head(
                self._get_head_locked(stream_id),
                expected_head,
            )
            parent = self._get_version_locked(actual.version_id)
            if parent is None:
                raise StrategyVersionStoreIntegrityError(
                    "strategy head references a missing version"
                )
            version = create_strategy_version(
                stream_id=stream_id,
                owner_scope=parent.owner_scope,
                version_number=parent.version_number + 1,
                result=result,
                source_context=source_context,
                parent=parent,
                created_at=created_at,
            )
            event = create_strategy_state_event(
                stream_id=stream_id,
                sequence=actual.revision + 1,
                version_id=version.version_id,
                state=initial_state_for_version(version),
                prior_event_id=actual.event_id,
                occurred_at=created_at,
            )
            self._insert_version(version)
            self._insert_event(event)
            head = self._replace_head(event)
        return version, head

    def get_confirmation_card(
        self,
        confirmation_hash: str,
    ) -> StrategyConfirmationCard | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT confirmation_hash, stream_id, version_id, payload_json
                FROM strategy_confirmation_cards
                WHERE confirmation_hash=?
                """,
                (confirmation_hash,),
            ).fetchone()
            if row is None:
                return None
            card = self._parse(
                StrategyConfirmationCard,
                row["payload_json"],
            )
            if (
                card.confirmation_hash != row["confirmation_hash"]
                or card.stream_id != row["stream_id"]
                or card.version_id != row["version_id"]
            ):
                raise StrategyVersionStoreIntegrityError(
                    "confirmation card index columns do not match payload"
                )
            return card

    def prepare_confirmation(
        self,
        *,
        stream_id: str,
        expected_head: StrategyHeadToken,
        issued_at: datetime,
        expires_at: datetime,
    ) -> tuple[StrategyConfirmationCard, StrategyHeadToken]:
        if (
            issued_at.tzinfo is None
            or issued_at.utcoffset() is None
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
        ):
            raise ValueError("confirmation timestamps must be timezone-aware")
        issued_at = issued_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        with self._lock, self._write_transaction():
            actual = self._require_expected_head(
                self._get_head_locked(stream_id),
                expected_head,
            )
            if actual.state not in {"draft", "awaiting_confirmation"}:
                raise InvalidStrategyTransitionError(
                    f"cannot prepare confirmation from {actual.state}"
                )
            if actual.state == "awaiting_confirmation":
                current_event = self._get_event_locked(actual.event_id)
                if (
                    current_event is None
                    or current_event.confirmation_hash is None
                ):
                    raise StrategyVersionStoreIntegrityError(
                        "awaiting head has no confirmation event"
                    )
                current_card = self.get_confirmation_card(
                    current_event.confirmation_hash
                )
                if current_card is None:
                    raise StrategyVersionStoreIntegrityError(
                        "awaiting head has no confirmation card"
                    )
                if issued_at < current_card.expires_at:
                    raise InvalidStrategyTransitionError(
                        "current confirmation card has not expired"
                    )
            version = self._get_version_locked(actual.version_id)
            if version is None:
                raise StrategyVersionStoreIntegrityError(
                    "strategy head references a missing version"
                )
            card = create_strategy_confirmation_card(
                version,
                stream_id=stream_id,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            event = create_strategy_state_event(
                stream_id=stream_id,
                sequence=actual.revision + 1,
                version_id=version.version_id,
                state="awaiting_confirmation",
                prior_event_id=actual.event_id,
                confirmation_hash=card.confirmation_hash,
                occurred_at=issued_at,
            )
            self._conn.execute(
                """
                INSERT INTO strategy_confirmation_cards (
                    confirmation_hash, stream_id, version_id, payload_json,
                    issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    card.confirmation_hash,
                    stream_id,
                    version.version_id,
                    self._json(card),
                    card.issued_at.isoformat(),
                    card.expires_at.isoformat(),
                ),
            )
            self._insert_event(event)
            head = self._replace_head(event)
        return card, head

    def _get_receipt_by_idempotency_key(
        self,
        stream_id: str,
        idempotency_key: str,
    ) -> StrategyConfirmationReceipt | None:
        row = self._conn.execute(
            """
            SELECT stream_id, version_id, confirmation_hash, idempotency_key,
                   actor_id, payload_json
            FROM strategy_confirmation_receipts
            WHERE stream_id=? AND idempotency_key=?
            """,
            (stream_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        receipt = self._parse(
            StrategyConfirmationReceipt,
            row["payload_json"],
        )
        if (
            receipt.stream_id != row["stream_id"]
            or receipt.version_id != row["version_id"]
            or receipt.confirmation_hash != row["confirmation_hash"]
            or receipt.idempotency_key != row["idempotency_key"]
            or receipt.actor_id != row["actor_id"]
        ):
            raise StrategyVersionStoreIntegrityError(
                "confirmation receipt index columns do not match payload"
            )
        return receipt

    def get_confirmation_receipt(
        self,
        receipt_id: str,
    ) -> StrategyConfirmationReceipt | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json
                FROM strategy_confirmation_receipts
                WHERE receipt_id=?
                """,
                (receipt_id,),
            ).fetchone()
            if row is None:
                return None
            receipt = self._parse(
                StrategyConfirmationReceipt,
                row["payload_json"],
            )
            if receipt.receipt_id != receipt_id:
                raise StrategyVersionStoreIntegrityError(
                    "confirmation receipt ID does not match payload"
                )
            return receipt

    def confirm(
        self,
        *,
        stream_id: str,
        expected_head: StrategyHeadToken,
        confirmation_hash: str,
        idempotency_key: str,
        actor_id: str,
        confirmed_at: datetime | None = None,
    ) -> StrategyConfirmationReceipt:
        if confirmed_at is not None and (
            confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None
        ):
            raise ValueError("confirmed_at must be timezone-aware")
        now = (
            confirmed_at.astimezone(timezone.utc)
            if confirmed_at is not None
            else datetime.now(timezone.utc)
        )
        with self._lock, self._write_transaction():
            existing = self._get_receipt_by_idempotency_key(
                stream_id,
                idempotency_key,
            )
            if existing is not None:
                if (
                    expected_head.stream_id != stream_id
                    or existing.version_id != expected_head.version_id
                    or existing.confirmation_hash != confirmation_hash
                    or existing.actor_id != actor_id
                ):
                    raise StrategyIdempotencyConflict(
                        "idempotency key was used for another confirmation"
                    )
                return existing

            actual = self._require_expected_head(
                self._get_head_locked(stream_id),
                expected_head,
            )
            if actual.state != "awaiting_confirmation":
                raise InvalidStrategyTransitionError(
                    f"cannot confirm from {actual.state}"
                )
            current_event = self._get_event_locked(actual.event_id)
            if (
                current_event is None
                or current_event.confirmation_hash != confirmation_hash
            ):
                raise StrategyConfirmationHashMismatch(
                    "confirmation hash does not match current card"
                )
            card = self.get_confirmation_card(confirmation_hash)
            if card is None:
                raise StrategyVersionStoreIntegrityError(
                    "current confirmation card is missing"
                )
            if now >= card.expires_at:
                raise ExpiredStrategyConfirmationError(
                    "confirmation card has expired"
                )
            receipt = create_strategy_confirmation_receipt(
                stream_id=stream_id,
                version_id=actual.version_id,
                confirmation_hash=confirmation_hash,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                confirmed_at=now,
            )
            event = create_strategy_state_event(
                stream_id=stream_id,
                sequence=actual.revision + 1,
                version_id=actual.version_id,
                state="confirmed",
                prior_event_id=actual.event_id,
                confirmation_hash=confirmation_hash,
                occurred_at=now,
            )
            self._conn.execute(
                """
                INSERT INTO strategy_confirmation_receipts (
                    receipt_id, stream_id, version_id, confirmation_hash,
                    idempotency_key, actor_id, payload_json, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    stream_id,
                    receipt.version_id,
                    receipt.confirmation_hash,
                    receipt.idempotency_key,
                    receipt.actor_id,
                    self._json(receipt),
                    receipt.confirmed_at.isoformat(),
                ),
            )
            self._insert_event(event)
            self._replace_head(event)
            return receipt
