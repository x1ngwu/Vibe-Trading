"""Strict QE4 strategy confirmation visualization and artifact helpers."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import DataSnapshotRef, ObjectRef, canonical_json

from .version_store import StrategyVersionStore
from .versioning import (
    StrategyConfirmationCard,
    StrategyConfirmationReceipt,
    StrategyHeadToken,
    StrategyVersion,
)

StrategyPresentationState = Literal[
    "needs_clarification",
    "awaiting_confirmation",
    "confirmed",
    "expired",
    "superseded",
]

_SAFE_ID = r"^[A-Za-z0-9_-]{1,128}$"
_STREAM_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_VERSION_ID = r"^strategy-version:[0-9a-f]{64}$"
_EVENT_ID = r"^strategy-state:[0-9a-f]{64}$"
_SHA256 = r"^[0-9a-f]{64}$"


class _PresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyConfirmationVisualizationSpec(_PresentationModel):
    """Small trusted metadata persisted in chat history and SSE."""

    schema_version: Literal[1] = 1
    type: Literal["strategy_confirmation"] = "strategy_confirmation"
    visualization_id: str = Field(pattern=_SAFE_ID)
    data_ref: str = Field(pattern=_SAFE_ID)
    title: str = Field(min_length=1, max_length=200)
    stream_id: str = Field(pattern=_STREAM_ID)
    version_id: str = Field(pattern=_VERSION_ID)
    version_number: int = Field(ge=1)
    parent_version_id: str | None = Field(default=None, pattern=_VERSION_ID)
    head_event_id: str = Field(pattern=_EVENT_ID)
    head_revision: int = Field(ge=1)
    confirmation_hash: str | None = Field(default=None, pattern=_SHA256)
    fallback_text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_identity(self) -> "StrategyConfirmationVisualizationSpec":
        if self.data_ref != self.visualization_id:
            raise ValueError("data_ref must equal visualization_id")
        if self.version_number == 1 and self.parent_version_id is not None:
            raise ValueError("initial strategy version cannot have a parent")
        if self.version_number > 1 and self.parent_version_id is None:
            raise ValueError("child strategy version requires parent")
        return self


class StrategyDataBasis(_PresentationModel):
    """Visible immutable market-data basis for the confirmation card."""

    snapshot_ref: ObjectRef
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    frequency: Literal["1d"]
    adjustment: Literal["raw", "qfq", "hfq"]
    requested_sources: tuple[str, ...] = Field(min_length=1)
    actual_sources: dict[str, str]
    anomalies: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(
        cls,
        snapshot_ref: ObjectRef,
        snapshot: DataSnapshotRef,
    ) -> "StrategyDataBasis":
        return cls(
            snapshot_ref=snapshot_ref,
            as_of=snapshot.as_of.isoformat(),
            start_date=snapshot.start_date.isoformat(),
            end_date=snapshot.end_date.isoformat(),
            frequency=snapshot.frequency,
            adjustment=snapshot.adjustment,
            requested_sources=snapshot.requested_sources,
            actual_sources=snapshot.actual_sources,
            anomalies=snapshot.anomalies,
        )


class StrategyConfirmationVisualizationPayload(_PresentationModel):
    """Full bounded card payload fetched by the chat UI."""

    schema_version: Literal[1] = 1
    type: Literal["strategy_confirmation"] = "strategy_confirmation"
    visualization_id: str = Field(pattern=_SAFE_ID)
    stream_id: str = Field(pattern=_STREAM_ID)
    lifecycle_state: StrategyPresentationState
    version: StrategyVersion
    head: StrategyHeadToken
    data_basis: StrategyDataBasis | None = None
    card: StrategyConfirmationCard | None = None
    receipt: StrategyConfirmationReceipt | None = None

    @model_validator(mode="after")
    def validate_chain(self) -> "StrategyConfirmationVisualizationPayload":
        if self.version.stream_id != self.stream_id or self.head.stream_id != self.stream_id:
            raise ValueError("strategy payload stream identity mismatch")
        if self.lifecycle_state != "superseded" and self.head.version_id != self.version.version_id:
            raise ValueError("current strategy payload must reference the head version")
        if self.card is not None:
            if (
                self.card.stream_id != self.stream_id
                or self.card.version_id != self.version.version_id
                or self.version.strategy_spec_ref != self.card.strategy_spec_ref
            ):
                raise ValueError("confirmation card does not bind the displayed version")
        if self.lifecycle_state in {"awaiting_confirmation", "expired", "confirmed"}:
            if self.card is None or self.data_basis is None:
                raise ValueError("confirmation lifecycle requires a card and data basis")
            if self.data_basis.snapshot_ref != self.card.strategy.data_snapshot_ref:
                raise ValueError("data basis does not bind the displayed strategy")
        if self.lifecycle_state == "needs_clarification":
            if self.version.draft_status != "needs_clarification" or self.card is not None:
                raise ValueError("clarification payload cannot contain a card")
        if self.receipt is not None:
            if self.card is None or (
                self.receipt.stream_id != self.stream_id
                or self.receipt.version_id != self.version.version_id
                or self.receipt.confirmation_hash != self.card.confirmation_hash
            ):
                raise ValueError("confirmation receipt does not bind the displayed card")
        return self


def default_strategy_version_db_path() -> Path:
    """Use the existing backed-up household research volume."""

    return Path.home() / ".vibe-trading" / "research" / "strategy_versions.db"


def strategy_visualization_id(
    version: StrategyVersion,
    card: StrategyConfirmationCard | None,
) -> str:
    suffix = card.confirmation_hash[:12] if card else "clarification"
    return f"strategy_{version.content_sha256[:20]}_{suffix}"


def build_strategy_visualization(
    *,
    version: StrategyVersion,
    head: StrategyHeadToken,
    card: StrategyConfirmationCard | None = None,
    receipt: StrategyConfirmationReceipt | None = None,
    data_basis: StrategyDataBasis | None = None,
    now: datetime | None = None,
) -> tuple[StrategyConfirmationVisualizationSpec, StrategyConfirmationVisualizationPayload]:
    """Build a mutually bound chat spec and full payload."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if head.version_id != version.version_id:
        state: StrategyPresentationState = "superseded"
    elif version.draft_status == "needs_clarification":
        state = "needs_clarification"
    elif head.state == "confirmed":
        state = "confirmed"
    elif card is not None and current >= card.expires_at:
        state = "expired"
    else:
        state = "awaiting_confirmation"
    visualization_id = strategy_visualization_id(version, card)
    title = version.strategy.title if version.strategy is not None else "策略需要补充信息"
    spec = StrategyConfirmationVisualizationSpec(
        visualization_id=visualization_id,
        data_ref=visualization_id,
        title=title,
        stream_id=version.stream_id,
        version_id=version.version_id,
        version_number=version.version_number,
        parent_version_id=version.parent_version_id,
        head_event_id=head.event_id,
        head_revision=head.revision,
        confirmation_hash=card.confirmation_hash if card is not None else None,
        fallback_text="策略确认卡不可用，请刷新或重新编制。",
    )
    payload = StrategyConfirmationVisualizationPayload(
        visualization_id=visualization_id,
        stream_id=version.stream_id,
        lifecycle_state=state,
        version=version,
        head=head,
        data_basis=data_basis,
        card=card,
        receipt=receipt,
    )
    return spec, payload


def resolve_strategy_visualization(
    payload: StrategyConfirmationVisualizationPayload,
    store: StrategyVersionStore,
    *,
    now: datetime | None = None,
) -> StrategyConfirmationVisualizationPayload:
    """Project an old artifact against the canonical current head."""

    head = store.get_head(payload.stream_id)
    if head is None:
        raise ValueError("strategy stream no longer exists")
    version = store.get_version(payload.version.version_id)
    if version is None:
        raise ValueError("strategy version no longer exists")
    card = payload.card
    if card is not None:
        canonical_card = store.get_confirmation_card(card.confirmation_hash)
        if canonical_card is None:
            raise ValueError("strategy confirmation card no longer exists")
        card = canonical_card
    _, resolved = build_strategy_visualization(
        version=version,
        head=head,
        card=card,
        receipt=payload.receipt,
        data_basis=payload.data_basis,
        now=now,
    )
    return resolved


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def persist_strategy_visualization(
    run_dir: Path,
    spec: StrategyConfirmationVisualizationSpec,
    payload: StrategyConfirmationVisualizationPayload,
) -> None:
    """Atomically persist a card and merge its small spec into the manifest."""

    artifact_dir = run_dir / "artifacts"
    _atomic_write_json(
        artifact_dir / "visualizations" / f"{spec.visualization_id}.json",
        payload.model_dump(mode="json"),
    )
    manifest_path = artifact_dir / "visualizations.json"
    manifest: list[dict[str, Any]] = []
    try:
        if manifest_path.is_file() and manifest_path.stat().st_size <= 256_000:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                manifest = [item for item in raw if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError):
        manifest = []
    serialized = spec.model_dump(mode="json")
    manifest = [
        item
        for item in manifest
        if item.get("visualization_id") != spec.visualization_id
    ]
    manifest.append(serialized)
    _atomic_write_json(manifest_path, manifest[-5:])
