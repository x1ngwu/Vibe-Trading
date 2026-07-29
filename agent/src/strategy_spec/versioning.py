"""Immutable QE4 strategy versions, diffs, cards, and lifecycle records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from src.research.contracts import (
    ObjectRef,
    StrategySpec,
    canonical_json,
    canonical_sha256,
)

from .drafting import (
    DraftDefaultDisclosure,
    DraftSecurityWarning,
    StrategyClarification,
    StrategyDraftProposal,
    StrategyDraftResult,
)

STRATEGY_VERSION_SCHEMA = "vibe.strategy-version.v1"
STRATEGY_CONFIRMATION_SCHEMA = "vibe.strategy-confirmation.v1"

StrategyLifecycleState = Literal[
    "draft",
    "needs_clarification",
    "awaiting_confirmation",
    "confirmed",
]
StrategyDiffKind = Literal["add", "remove", "replace"]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_VERSION_ID_PATTERN = r"^strategy-version:[0-9a-f]{64}$"
_EVENT_ID_PATTERN = r"^strategy-state:[0-9a-f]{64}$"
_CARD_ID_PATTERN = r"^strategy-confirmation:[0-9a-f]{64}$"
_RECEIPT_ID_PATTERN = r"^strategy-receipt:[0-9a-f]{64}$"
_STREAM_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class StrategyVersionError(ValueError):
    """Base error for deterministic version and confirmation failures."""


class StaleStrategyHeadError(StrategyVersionError):
    """Raised when a caller writes against an outdated head token."""


class InvalidStrategyTransitionError(StrategyVersionError):
    """Raised when a lifecycle transition is not allowed."""


class ExpiredStrategyConfirmationError(StrategyVersionError):
    """Raised when a confirmation card is past its fixed expiry."""


class StrategyConfirmationHashMismatch(StrategyVersionError):
    """Raised when confirmation does not bind the current exact card."""


class StrategyIdempotencyConflict(StrategyVersionError):
    """Raised when one idempotency key is reused for different semantics."""


class _VersionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class StrategyDiffEntry(_VersionModel):
    """One deterministic leaf change between immutable versions."""

    path: str = Field(pattern=r"^\$.*", max_length=500)
    kind: StrategyDiffKind
    before_json: str | None = Field(default=None, max_length=20_000)
    after_json: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def validate_sides(self) -> "StrategyDiffEntry":
        if self.kind == "add" and (
            self.before_json is not None or self.after_json is None
        ):
            raise ValueError("add diff requires only after_json")
        if self.kind == "remove" and (
            self.before_json is None or self.after_json is not None
        ):
            raise ValueError("remove diff requires only before_json")
        if self.kind == "replace" and (
            self.before_json is None
            or self.after_json is None
            or self.before_json == self.after_json
        ):
            raise ValueError("replace diff requires two different values")
        return self


class StrategyVersion(_VersionModel):
    """Immutable semantic draft version; lifecycle state is stored separately."""

    schema_version: Literal["vibe.strategy-version.v1"] = STRATEGY_VERSION_SCHEMA
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    stream_id: str = Field(pattern=_STREAM_PATTERN)
    owner_scope: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    version_number: int = Field(ge=1)
    parent_version_id: str | None = Field(
        default=None,
        pattern=_VERSION_ID_PATTERN,
    )
    draft_status: Literal["ready", "needs_clarification"]
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_response_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal: StrategyDraftProposal
    strategy_spec_ref: ObjectRef | None = None
    strategy: StrategySpec | None = None
    defaults: tuple[DraftDefaultDisclosure, ...] = ()
    clarifications: tuple[StrategyClarification, ...] = ()
    security_warnings: tuple[DraftSecurityWarning, ...] = ()
    diff: tuple[StrategyDiffEntry, ...] = ()
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_version(self) -> "StrategyVersion":
        if self.version_number == 1 and self.parent_version_id is not None:
            raise ValueError("initial version cannot have a parent")
        if self.version_number > 1 and self.parent_version_id is None:
            raise ValueError("child version requires parent_version_id")
        if self.draft_status == "ready":
            if self.strategy_spec_ref is None or self.strategy is None:
                raise ValueError("ready version requires strategy content")
            if self.clarifications:
                raise ValueError("ready version cannot require clarification")
        else:
            if self.strategy_spec_ref is not None or self.strategy is not None:
                raise ValueError(
                    "needs_clarification version cannot contain strategy content"
                )
            if not self.clarifications:
                raise ValueError(
                    "needs_clarification version requires questions"
                )
        if self.strategy_spec_ref is not None:
            if self.strategy_spec_ref.object_type != "strategy_spec":
                raise ValueError("strategy_spec_ref has the wrong object type")
            assert self.strategy is not None
            if self.strategy.data_snapshot_ref.object_type != "data_snapshot_ref":
                raise ValueError("strategy snapshot ref has the wrong object type")
        material = strategy_version_material(self)
        expected = canonical_sha256(material)
        if self.content_sha256 != expected:
            raise ValueError("version content_sha256 does not match content")
        if self.version_id != f"strategy-version:{expected}":
            raise ValueError("version_id does not match content")
        return self


class StrategyStateEvent(_VersionModel):
    """Append-only lifecycle transition for one immutable version."""

    event_id: str = Field(pattern=_EVENT_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    stream_id: str = Field(pattern=_STREAM_PATTERN)
    sequence: int = Field(ge=1)
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    prior_event_id: str | None = Field(default=None, pattern=_EVENT_ID_PATTERN)
    state: StrategyLifecycleState
    confirmation_hash: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_event(self) -> "StrategyStateEvent":
        if self.sequence == 1 and self.prior_event_id is not None:
            raise ValueError("initial state event cannot have a prior event")
        if self.sequence > 1 and self.prior_event_id is None:
            raise ValueError("later state event requires prior_event_id")
        if self.state in {"awaiting_confirmation", "confirmed"}:
            if self.confirmation_hash is None:
                raise ValueError(
                    "confirmation lifecycle state requires confirmation_hash"
                )
        elif self.confirmation_hash is not None:
            raise ValueError(
                "draft/clarification state cannot carry confirmation_hash"
            )
        material = strategy_state_event_material(self)
        expected = canonical_sha256(material)
        if self.content_sha256 != expected:
            raise ValueError("event content_sha256 does not match content")
        if self.event_id != f"strategy-state:{expected}":
            raise ValueError("event_id does not match content")
        return self


class StrategyHeadToken(_VersionModel):
    """Optimistic concurrency token changed by every state transition."""

    stream_id: str = Field(pattern=_STREAM_PATTERN)
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    event_id: str = Field(pattern=_EVENT_ID_PATTERN)
    revision: int = Field(ge=1)
    state: StrategyLifecycleState


class StrategyConfirmationCard(_VersionModel):
    """Exact, expiring content the user is asked to confirm."""

    schema_version: Literal[
        "vibe.strategy-confirmation.v1"
    ] = STRATEGY_CONFIRMATION_SCHEMA
    card_id: str = Field(pattern=_CARD_ID_PATTERN)
    confirmation_hash: str = Field(pattern=_SHA256_PATTERN)
    stream_id: str = Field(pattern=_STREAM_PATTERN)
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    version_number: int = Field(ge=1)
    parent_version_id: str | None = Field(
        default=None,
        pattern=_VERSION_ID_PATTERN,
    )
    strategy_spec_ref: ObjectRef
    strategy: StrategySpec
    defaults: tuple[DraftDefaultDisclosure, ...] = ()
    security_warnings: tuple[DraftSecurityWarning, ...] = ()
    diff: tuple[StrategyDiffEntry, ...] = ()
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_card(self) -> "StrategyConfirmationCard":
        if self.version_number == 1 and self.parent_version_id is not None:
            raise ValueError("initial confirmation version cannot have parent")
        if self.version_number > 1 and self.parent_version_id is None:
            raise ValueError("child confirmation version requires parent")
        if self.strategy_spec_ref.object_type != "strategy_spec":
            raise ValueError("confirmation card must reference strategy_spec")
        if self.expires_at <= self.issued_at:
            raise ValueError("confirmation card expiry must follow issue time")
        material = strategy_confirmation_material(self)
        expected = canonical_sha256(material)
        if self.confirmation_hash != expected:
            raise ValueError("confirmation_hash does not match card content")
        if self.card_id != f"strategy-confirmation:{expected}":
            raise ValueError("card_id does not match card content")
        return self


class StrategyConfirmationReceipt(_VersionModel):
    """Idempotent evidence that one exact version/card was confirmed."""

    receipt_id: str = Field(pattern=_RECEIPT_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    stream_id: str = Field(pattern=_STREAM_PATTERN)
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    confirmation_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotency_key: str = Field(pattern=_STREAM_PATTERN)
    actor_id: str = Field(pattern=_STREAM_PATTERN)
    confirmed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_receipt(self) -> "StrategyConfirmationReceipt":
        material = strategy_confirmation_receipt_material(self)
        expected = canonical_sha256(material)
        if self.content_sha256 != expected:
            raise ValueError("receipt content_sha256 does not match content")
        if self.receipt_id != f"strategy-receipt:{expected}":
            raise ValueError("receipt_id does not match content")
        return self


def _without_identity(
    value: BaseModel,
    *,
    excluded: set[str],
) -> Mapping[str, Any]:
    return value.model_dump(mode="json", exclude=excluded)


def strategy_version_material(version: StrategyVersion) -> Mapping[str, Any]:
    """Return identity material, excluding audit time and identity fields."""

    return _without_identity(
        version,
        excluded={"version_id", "content_sha256", "created_at"},
    )


def strategy_state_event_material(
    event: StrategyStateEvent,
) -> Mapping[str, Any]:
    return _without_identity(
        event,
        excluded={"event_id", "content_sha256"},
    )


def strategy_confirmation_material(
    card: StrategyConfirmationCard,
) -> Mapping[str, Any]:
    return _without_identity(
        card,
        excluded={"card_id", "confirmation_hash"},
    )


def strategy_confirmation_receipt_material(
    receipt: StrategyConfirmationReceipt,
) -> Mapping[str, Any]:
    return _without_identity(
        receipt,
        excluded={"receipt_id", "content_sha256"},
    )


_MISSING = object()


def _diff_values(
    before: Any,
    after: Any,
    *,
    path: str,
    output: list[StrategyDiffEntry],
) -> None:
    if before is _MISSING:
        output.append(
            StrategyDiffEntry(
                path=path,
                kind="add",
                after_json=canonical_json(after),
            )
        )
        return
    if after is _MISSING:
        output.append(
            StrategyDiffEntry(
                path=path,
                kind="remove",
                before_json=canonical_json(before),
            )
        )
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            _diff_values(
                before.get(key, _MISSING),
                after.get(key, _MISSING),
                path=f"{path}.{key}",
                output=output,
            )
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            _diff_values(
                before[index] if index < len(before) else _MISSING,
                after[index] if index < len(after) else _MISSING,
                path=f"{path}[{index}]",
                output=output,
            )
        return
    if before != after:
        output.append(
            StrategyDiffEntry(
                path=path,
                kind="replace",
                before_json=canonical_json(before),
                after_json=canonical_json(after),
            )
        )


def _semantic_document_from_result(
    result: StrategyDraftResult,
) -> Mapping[str, Any]:
    strategy = (
        result.build.strategy_object.payload.model_dump(mode="json")
        if result.build is not None
        else None
    )
    return {
        "draft_status": result.status,
        "proposal": result.proposal.model_dump(mode="json"),
        "strategy": strategy,
        "defaults": [
            item.model_dump(mode="json") for item in result.defaults
        ],
        "clarifications": [
            item.model_dump(mode="json") for item in result.clarifications
        ],
        "security_warnings": [
            item.model_dump(mode="json") for item in result.security_warnings
        ],
    }


def diff_strategy_results(
    before: StrategyVersion,
    after: StrategyDraftResult,
) -> tuple[StrategyDiffEntry, ...]:
    """Return a stable leaf diff from an existing version to a new result."""

    before_document = {
        "draft_status": before.draft_status,
        "proposal": before.proposal.model_dump(mode="json"),
        "strategy": (
            before.strategy.model_dump(mode="json")
            if before.strategy is not None
            else None
        ),
        "defaults": [
            item.model_dump(mode="json") for item in before.defaults
        ],
        "clarifications": [
            item.model_dump(mode="json") for item in before.clarifications
        ],
        "security_warnings": [
            item.model_dump(mode="json") for item in before.security_warnings
        ],
    }
    output: list[StrategyDiffEntry] = []
    _diff_values(
        before_document,
        _semantic_document_from_result(after),
        path="$",
        output=output,
    )
    return tuple(sorted(output, key=lambda item: (item.path, item.kind)))


def create_strategy_version(
    *,
    stream_id: str,
    owner_scope: str,
    version_number: int,
    result: StrategyDraftResult,
    parent: StrategyVersion | None = None,
    created_at: datetime | None = None,
) -> StrategyVersion:
    """Create one immutable version from a non-rejected draft result."""

    if result.status == "rejected":
        raise StrategyVersionError("rejected draft cannot create a version")
    if parent is None and version_number != 1:
        raise StrategyVersionError("initial version_number must be 1")
    if parent is not None:
        if parent.stream_id != stream_id or parent.owner_scope != owner_scope:
            raise StrategyVersionError("parent crosses stream or owner scope")
        if version_number != parent.version_number + 1:
            raise StrategyVersionError("child version_number must increment by one")
    strategy_ref = None
    strategy = None
    if result.status == "ready":
        assert result.build is not None
        strategy_object = result.build.strategy_object
        if strategy_object.owner_scope != owner_scope:
            raise StrategyVersionError("draft strategy crosses owner scope")
        strategy_ref = strategy_object.ref()
        assert isinstance(strategy_object.payload, StrategySpec)
        strategy = strategy_object.payload
    diff = diff_strategy_results(parent, result) if parent is not None else ()
    payload = {
        "schema_version": STRATEGY_VERSION_SCHEMA,
        "stream_id": stream_id,
        "owner_scope": owner_scope,
        "version_number": version_number,
        "parent_version_id": parent.version_id if parent is not None else None,
        "draft_status": result.status,
        "request_sha256": result.request_sha256,
        "model_response_sha256": result.model_response_sha256,
        "proposal": result.proposal.model_dump(mode="json"),
        "strategy_spec_ref": (
            strategy_ref.model_dump(mode="json") if strategy_ref else None
        ),
        "strategy": strategy.model_dump(mode="json") if strategy else None,
        "defaults": [
            item.model_dump(mode="json") for item in result.defaults
        ],
        "clarifications": [
            item.model_dump(mode="json") for item in result.clarifications
        ],
        "security_warnings": [
            item.model_dump(mode="json") for item in result.security_warnings
        ],
        "diff": [item.model_dump(mode="json") for item in diff],
    }
    digest = canonical_sha256(payload)
    return StrategyVersion(
        **payload,
        version_id=f"strategy-version:{digest}",
        content_sha256=digest,
        created_at=_utc(created_at or datetime.now(timezone.utc)),
    )


def create_strategy_state_event(
    *,
    stream_id: str,
    sequence: int,
    version_id: str,
    state: StrategyLifecycleState,
    prior_event_id: str | None,
    confirmation_hash: str | None = None,
    occurred_at: datetime | None = None,
) -> StrategyStateEvent:
    payload = {
        "stream_id": stream_id,
        "sequence": sequence,
        "version_id": version_id,
        "prior_event_id": prior_event_id,
        "state": state,
        "confirmation_hash": confirmation_hash,
        "occurred_at": _utc(occurred_at or datetime.now(timezone.utc)),
    }
    digest = canonical_sha256(payload)
    return StrategyStateEvent(
        **payload,
        event_id=f"strategy-state:{digest}",
        content_sha256=digest,
    )


def create_strategy_confirmation_card(
    version: StrategyVersion,
    *,
    stream_id: str,
    issued_at: datetime,
    expires_at: datetime,
) -> StrategyConfirmationCard:
    if version.stream_id != stream_id:
        raise StrategyVersionError("version belongs to another stream")
    if version.draft_status != "ready":
        raise InvalidStrategyTransitionError(
            "only ready versions can enter confirmation"
        )
    assert version.strategy_spec_ref is not None
    assert version.strategy is not None
    payload = {
        "schema_version": STRATEGY_CONFIRMATION_SCHEMA,
        "stream_id": stream_id,
        "version_id": version.version_id,
        "version_number": version.version_number,
        "parent_version_id": version.parent_version_id,
        "strategy_spec_ref": version.strategy_spec_ref.model_dump(mode="json"),
        "strategy": version.strategy.model_dump(mode="json"),
        "defaults": [
            item.model_dump(mode="json") for item in version.defaults
        ],
        "security_warnings": [
            item.model_dump(mode="json")
            for item in version.security_warnings
        ],
        "diff": [item.model_dump(mode="json") for item in version.diff],
        "issued_at": _utc(issued_at),
        "expires_at": _utc(expires_at),
    }
    digest = canonical_sha256(payload)
    return StrategyConfirmationCard(
        **payload,
        card_id=f"strategy-confirmation:{digest}",
        confirmation_hash=digest,
    )


def create_strategy_confirmation_receipt(
    *,
    stream_id: str,
    version_id: str,
    confirmation_hash: str,
    idempotency_key: str,
    actor_id: str,
    confirmed_at: datetime,
) -> StrategyConfirmationReceipt:
    payload = {
        "stream_id": stream_id,
        "version_id": version_id,
        "confirmation_hash": confirmation_hash,
        "idempotency_key": idempotency_key,
        "actor_id": actor_id,
        "confirmed_at": _utc(confirmed_at),
    }
    digest = canonical_sha256(payload)
    return StrategyConfirmationReceipt(
        **payload,
        receipt_id=f"strategy-receipt:{digest}",
        content_sha256=digest,
    )


def initial_state_for_version(
    version: StrategyVersion,
) -> Literal["draft", "needs_clarification"]:
    return (
        "draft"
        if version.draft_status == "ready"
        else "needs_clarification"
    )
