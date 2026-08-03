"""Content-addressed QE6 reconciliation evidence and strategy validation gate.

Confirmation, backtest completion, and independent-oracle validation are
deliberately separate lifecycle facts.  This module persists the last one: an
immutable evidence closure for an exact QE5/vn.py comparison, including every
ordered checkpoint and the first divergence when one exists, plus a derived
(never caller-selected) validation decision for one strategy version.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Sequence

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from src.research.contracts import canonical_json, canonical_sha256


RECONCILIATION_ARTIFACT_SCHEMA = "vibe.reconciliation-artifact.v2"
STRATEGY_VALIDATION_DECISION_SCHEMA = "vibe.strategy-validation-decision.v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ARTIFACT_ID_PATTERN = r"^reconciliation-artifact:[0-9a-f]{64}$"
_DECISION_ID_PATTERN = r"^strategy-validation:[0-9a-f]{64}$"
_VERSION_ID_PATTERN = r"^strategy-version:[0-9a-f]{64}$"
_SCOPE_PATTERN = r"^[a-z][a-z0-9._:-]{0,127}$"
_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_SAFE_FILE_ID = re.compile(r"^[0-9a-f]{64}$")


class ReconciliationError(ValueError):
    """Base error for invalid reconciliation evidence or validation input."""


class ReconciliationIntegrityError(ReconciliationError):
    """Persisted evidence does not match its content identity or safe layout."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReconciliationEngineIdentity(_StrictModel):
    """Exact installed engine and audited source closure used by one side."""

    name: Literal["quantaxis", "vnpy"]
    version: str = Field(min_length=1, max_length=64)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_sha256: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_closure(self) -> "ReconciliationEngineIdentity":
        if any(not _SAFE_FILE_ID.fullmatch(value) for value in self.source_sha256.values()):
            raise ValueError("engine source closure contains an invalid SHA-256")
        return self


class ReconciliationAccountState(_StrictModel):
    """Integer-fen account state compared after one deterministic event."""

    cash_fen: int = Field(ge=0)
    dividend_receivable_fen: int = Field(ge=0)
    positions: dict[str, int]
    sellable_positions: dict[str, int]
    mark_prices_fen: dict[str, int]
    market_value_fen: int = Field(ge=0)
    equity_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_account(self) -> "ReconciliationAccountState":
        if any(quantity <= 0 for quantity in self.positions.values()):
            raise ValueError("positions must contain only positive quantities")
        if set(self.mark_prices_fen) != set(self.positions):
            raise ValueError("mark prices must cover every and only open positions")
        if any(price <= 0 for price in self.mark_prices_fen.values()):
            raise ValueError("mark prices must be positive integer fen")
        if any(
            quantity < 0
            or symbol not in self.positions
            or quantity > self.positions[symbol]
            for symbol, quantity in self.sellable_positions.items()
        ):
            raise ValueError("sellable positions must be a non-negative subset of positions")
        expected_market_value = sum(
            quantity * self.mark_prices_fen[symbol]
            for symbol, quantity in self.positions.items()
        )
        if self.market_value_fen != expected_market_value:
            raise ValueError("market value does not match positions and marks")
        if self.equity_fen != (
            self.cash_fen + self.dividend_receivable_fen + self.market_value_fen
        ):
            raise ValueError("equity does not match cash, receivables, and market value")
        return self


class ReconciliationObservation(_StrictModel):
    """One engine's event outcome and post-event account state."""

    event_output: dict[str, Any]
    account_state: ReconciliationAccountState


class ReconciliationCheckpoint(_StrictModel):
    """Both independent observations for one exact ordered input."""

    sequence: int = Field(ge=1)
    trade_date: date
    event: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    event_input: dict[str, Any]
    event_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    rule_version: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    qe5: ReconciliationObservation
    vnpy: ReconciliationObservation

    @model_validator(mode="after")
    def validate_input_identity(self) -> "ReconciliationCheckpoint":
        if self.event_input_sha256 != canonical_sha256(self.event_input):
            raise ValueError("event_input_sha256 does not match event_input")
        return self

    @property
    def matches(self) -> bool:
        return canonical_json(self.qe5) == canonical_json(self.vnpy)


class FirstDivergence(_StrictModel):
    """Complete evidence for the first unequal event/account observation."""

    sequence: int = Field(ge=1)
    trade_date: date
    event: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    event_input: dict[str, Any]
    event_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    rule_version: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    differing_fields: tuple[str, ...] = Field(min_length=1)
    qe5: ReconciliationObservation
    vnpy: ReconciliationObservation

    @model_validator(mode="after")
    def validate_divergence(self) -> "FirstDivergence":
        if self.event_input_sha256 != canonical_sha256(self.event_input):
            raise ValueError("divergence input hash does not match input")
        if tuple(sorted(set(self.differing_fields))) != self.differing_fields:
            raise ValueError("differing_fields must be sorted and unique")
        if canonical_json(self.qe5) == canonical_json(self.vnpy):
            raise ValueError("first divergence requires unequal observations")
        return self


class ReconciliationArtifactRef(_StrictModel):
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_ref(self) -> "ReconciliationArtifactRef":
        if self.artifact_id != f"reconciliation-artifact:{self.content_sha256}":
            raise ValueError("artifact reference identity mismatch")
        return self


class ReconciliationArtifact(_StrictModel):
    """Immutable comparison outcome bound to all execution identities."""

    schema_version: Literal["vibe.reconciliation-artifact.v2"] = RECONCILIATION_ARTIFACT_SCHEMA
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    owner_scope: str = Field(pattern=_SCOPE_PATTERN)
    stream_id: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    strategy_version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    engine_request_id: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    engine_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    qe5_backtest_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    qe5_ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    vnpy_replay_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    qe5_engine: ReconciliationEngineIdentity
    vnpy_engine: ReconciliationEngineIdentity
    checkpoints: tuple[ReconciliationCheckpoint, ...] = Field(min_length=1)
    comparison_sha256: str = Field(pattern=_SHA256_PATTERN)
    comparison_status: Literal["matched", "diverged"]
    compared_entries: int = Field(ge=1)
    first_divergence: FirstDivergence | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_artifact(self) -> "ReconciliationArtifact":
        if self.qe5_engine.name != "quantaxis" or self.vnpy_engine.name != "vnpy":
            raise ValueError("reconciliation engine roles are invalid")
        _validate_checkpoint_order(self.checkpoints)
        if self.compared_entries != len(self.checkpoints):
            raise ValueError("compared_entries does not match checkpoint evidence")
        if self.comparison_sha256 != canonical_sha256(self.checkpoints):
            raise ValueError("comparison_sha256 does not match checkpoint evidence")
        expected_divergence = _first_divergence(self.checkpoints)
        expected_status = "diverged" if expected_divergence is not None else "matched"
        if self.comparison_status != expected_status:
            raise ValueError("comparison_status does not match checkpoint evidence")
        if self.first_divergence != expected_divergence:
            raise ValueError("first_divergence does not match checkpoint evidence")
        expected = canonical_sha256(reconciliation_artifact_material(self))
        if self.content_sha256 != expected:
            raise ValueError("artifact content_sha256 does not match content")
        if self.artifact_id != f"reconciliation-artifact:{expected}":
            raise ValueError("artifact_id does not match content")
        return self

    def ref(self) -> ReconciliationArtifactRef:
        return ReconciliationArtifactRef(
            artifact_id=self.artifact_id,
            content_sha256=self.content_sha256,
        )


class StrategyValidationDecision(_StrictModel):
    """A strategy validation result derived solely from one stored artifact."""

    schema_version: Literal[
        "vibe.strategy-validation-decision.v1"
    ] = STRATEGY_VALIDATION_DECISION_SCHEMA
    decision_id: str = Field(pattern=_DECISION_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    owner_scope: str = Field(pattern=_SCOPE_PATTERN)
    stream_id: str = Field(pattern=_TOKEN_PATTERN, max_length=128)
    strategy_version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    status: Literal["validated", "blocked"]
    reason: Literal[
        "INDEPENDENT_ORACLE_MATCH",
        "FIRST_DIVERGENCE",
        "ARTIFACT_LINEAGE_MISMATCH",
    ]
    artifact_ref: ReconciliationArtifactRef
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def validate_decision(self) -> "StrategyValidationDecision":
        if (self.status == "validated") != (self.reason == "INDEPENDENT_ORACLE_MATCH"):
            raise ValueError("only an independent oracle match can validate a strategy")
        expected = canonical_sha256(strategy_validation_decision_material(self))
        if self.content_sha256 != expected:
            raise ValueError("decision content_sha256 does not match content")
        if self.decision_id != f"strategy-validation:{expected}":
            raise ValueError("decision_id does not match content")
        return self


def reconciliation_artifact_material(artifact: ReconciliationArtifact) -> Mapping[str, Any]:
    return artifact.model_dump(
        mode="json",
        exclude={"artifact_id", "content_sha256", "created_at"},
    )


def strategy_validation_decision_material(
    decision: StrategyValidationDecision,
) -> Mapping[str, Any]:
    return decision.model_dump(
        mode="json",
        exclude={"decision_id", "content_sha256", "decided_at"},
    )


def _different_fields(
    qe5: ReconciliationObservation,
    vnpy: ReconciliationObservation,
) -> tuple[str, ...]:
    left = qe5.model_dump(mode="json")
    right = vnpy.model_dump(mode="json")
    differences: list[str] = []
    for section in ("event_output", "account_state"):
        left_section = left[section]
        right_section = right[section]
        for key in sorted(set(left_section) | set(right_section)):
            if left_section.get(key) != right_section.get(key):
                differences.append(f"{section}.{key}")
    return tuple(sorted(differences))


def _validate_checkpoint_order(
    checkpoints: Sequence[ReconciliationCheckpoint],
) -> tuple[ReconciliationCheckpoint, ...]:
    normalized = tuple(checkpoints)
    if not normalized:
        raise ReconciliationError("at least one reconciliation checkpoint is required")
    if tuple(item.sequence for item in normalized) != tuple(
        range(1, len(normalized) + 1)
    ):
        raise ReconciliationError(
            "checkpoint sequences must be contiguous and start at one"
        )
    if any(
        current.trade_date < previous.trade_date
        for previous, current in zip(normalized, normalized[1:])
    ):
        raise ReconciliationError("checkpoint dates must be non-decreasing")
    return normalized


def _first_divergence(
    checkpoints: Sequence[ReconciliationCheckpoint],
) -> FirstDivergence | None:
    divergent = next((item for item in checkpoints if not item.matches), None)
    if divergent is None:
        return None
    return FirstDivergence(
        sequence=divergent.sequence,
        trade_date=divergent.trade_date,
        event=divergent.event,
        event_input=divergent.event_input,
        event_input_sha256=divergent.event_input_sha256,
        rule_version=divergent.rule_version,
        differing_fields=_different_fields(divergent.qe5, divergent.vnpy),
        qe5=divergent.qe5,
        vnpy=divergent.vnpy,
    )


def create_reconciliation_artifact(
    *,
    owner_scope: str,
    stream_id: str,
    strategy_version_id: str,
    engine_request_id: str,
    engine_request_sha256: str,
    execution_plan_sha256: str,
    snapshot_sha256: str,
    qe5_backtest_input_sha256: str,
    qe5_ledger_sha256: str,
    vnpy_replay_input_sha256: str,
    qe5_engine: ReconciliationEngineIdentity,
    vnpy_engine: ReconciliationEngineIdentity,
    checkpoints: Sequence[ReconciliationCheckpoint],
    created_at: datetime | None = None,
) -> ReconciliationArtifact:
    """Compare and retain ordered checkpoints plus the derived first divergence."""

    normalized = _validate_checkpoint_order(checkpoints)
    first_divergence = _first_divergence(normalized)
    comparison_sha256 = canonical_sha256(normalized)
    material = {
        "schema_version": RECONCILIATION_ARTIFACT_SCHEMA,
        "owner_scope": owner_scope,
        "stream_id": stream_id,
        "strategy_version_id": strategy_version_id,
        "engine_request_id": engine_request_id,
        "engine_request_sha256": engine_request_sha256,
        "execution_plan_sha256": execution_plan_sha256,
        "snapshot_sha256": snapshot_sha256,
        "qe5_backtest_input_sha256": qe5_backtest_input_sha256,
        "qe5_ledger_sha256": qe5_ledger_sha256,
        "vnpy_replay_input_sha256": vnpy_replay_input_sha256,
        "qe5_engine": qe5_engine,
        "vnpy_engine": vnpy_engine,
        "checkpoints": normalized,
        "comparison_sha256": comparison_sha256,
        "comparison_status": "diverged" if first_divergence is not None else "matched",
        "compared_entries": len(normalized),
        "first_divergence": first_divergence,
    }
    digest = canonical_sha256(material)
    return ReconciliationArtifact(
        artifact_id=f"reconciliation-artifact:{digest}",
        content_sha256=digest,
        created_at=created_at or datetime.now(timezone.utc),
        **material,
    )


def decide_strategy_validation(
    artifact: ReconciliationArtifact,
    *,
    owner_scope: str,
    stream_id: str,
    strategy_version_id: str,
    decided_at: datetime | None = None,
) -> StrategyValidationDecision:
    """Derive the only allowed validation status; callers cannot choose it."""

    lineage_matches = (
        artifact.owner_scope == owner_scope
        and artifact.stream_id == stream_id
        and artifact.strategy_version_id == strategy_version_id
    )
    if not lineage_matches:
        status: Literal["validated", "blocked"] = "blocked"
        reason: Literal[
            "INDEPENDENT_ORACLE_MATCH",
            "FIRST_DIVERGENCE",
            "ARTIFACT_LINEAGE_MISMATCH",
        ] = "ARTIFACT_LINEAGE_MISMATCH"
    elif artifact.comparison_status == "diverged":
        status = "blocked"
        reason = "FIRST_DIVERGENCE"
    else:
        status = "validated"
        reason = "INDEPENDENT_ORACLE_MATCH"
    material = {
        "schema_version": STRATEGY_VALIDATION_DECISION_SCHEMA,
        "owner_scope": owner_scope,
        "stream_id": stream_id,
        "strategy_version_id": strategy_version_id,
        "status": status,
        "reason": reason,
        "artifact_ref": artifact.ref().model_dump(mode="json"),
    }
    digest = canonical_sha256(material)
    return StrategyValidationDecision(
        decision_id=f"strategy-validation:{digest}",
        content_sha256=digest,
        decided_at=decided_at or datetime.now(timezone.utc),
        **material,
    )


class ReconciliationStore:
    """Small immutable store for artifacts and their derived decisions."""

    def __init__(self, root: Path, *, max_object_bytes: int = 4_194_304) -> None:
        if max_object_bytes <= 0:
            raise ValueError("max_object_bytes must be positive")
        self.root = Path(root)
        self.max_object_bytes = max_object_bytes
        self.artifacts_dir = self.root / "artifacts"
        self.decisions_dir = self.root / "decisions"
        self.lock_path = self.root / ".reconciliation.lock"
        self._prepare_root()

    def put_artifact(self, artifact: ReconciliationArtifact) -> bool:
        return self._put(
            self.artifacts_dir / f"{artifact.content_sha256}.json",
            artifact,
            expected_id=artifact.artifact_id,
        )

    def get_artifact(
        self,
        artifact_id: str,
        *,
        owner_scope: str,
    ) -> ReconciliationArtifact | None:
        digest = self._parse_id(artifact_id, "reconciliation-artifact")
        path = self.artifacts_dir / f"{digest}.json"
        if not path.exists() and not path.is_symlink():
            return None
        artifact = self._load(path, ReconciliationArtifact)
        if artifact.artifact_id != artifact_id or artifact.content_sha256 != digest:
            raise ReconciliationIntegrityError("artifact identity does not match its path")
        return artifact if artifact.owner_scope == owner_scope else None

    def put_decision(self, decision: StrategyValidationDecision) -> bool:
        artifact = self.get_artifact(
            decision.artifact_ref.artifact_id,
            owner_scope=decision.owner_scope,
        )
        if artifact is None or artifact.ref() != decision.artifact_ref:
            raise ReconciliationIntegrityError("validation decision references a missing artifact")
        expected = decide_strategy_validation(
            artifact,
            owner_scope=decision.owner_scope,
            stream_id=decision.stream_id,
            strategy_version_id=decision.strategy_version_id,
            decided_at=decision.decided_at,
        )
        if expected != decision:
            raise ReconciliationIntegrityError("validation decision was not derived from its artifact")
        return self._put(
            self.decisions_dir / f"{decision.content_sha256}.json",
            decision,
            expected_id=decision.decision_id,
        )

    def get_decision(
        self,
        decision_id: str,
        *,
        owner_scope: str,
    ) -> StrategyValidationDecision | None:
        digest = self._parse_id(decision_id, "strategy-validation")
        path = self.decisions_dir / f"{digest}.json"
        if not path.exists() and not path.is_symlink():
            return None
        decision = self._load(path, StrategyValidationDecision)
        if decision.decision_id != decision_id or decision.content_sha256 != digest:
            raise ReconciliationIntegrityError("decision identity does not match its path")
        if decision.owner_scope != owner_scope:
            return None
        artifact = self.get_artifact(decision.artifact_ref.artifact_id, owner_scope=owner_scope)
        if artifact is None:
            raise ReconciliationIntegrityError("decision artifact is missing")
        expected = decide_strategy_validation(
            artifact,
            owner_scope=decision.owner_scope,
            stream_id=decision.stream_id,
            strategy_version_id=decision.strategy_version_id,
            decided_at=decision.decided_at,
        )
        if expected != decision:
            raise ReconciliationIntegrityError("stored decision is inconsistent with its artifact")
        return decision

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise ReconciliationIntegrityError("reconciliation root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for directory in (self.artifacts_dir, self.decisions_dir):
            if directory.is_symlink():
                raise ReconciliationIntegrityError("reconciliation directory must not be a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        if self.lock_path.is_symlink():
            raise ReconciliationIntegrityError("reconciliation lock must not be a symlink")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _put(self, path: Path, value: BaseModel, *, expected_id: str) -> bool:
        payload = (canonical_json(value) + "\n").encode("utf-8")
        if len(payload) > self.max_object_bytes:
            raise ReconciliationIntegrityError("reconciliation object exceeds size limit")
        with self._lock():
            if path.exists() or path.is_symlink():
                loaded_type = type(value)
                loaded = self._load(path, loaded_type)
                loaded_id = next(
                    (
                        getattr(loaded, field_name)
                        for field_name in ("artifact_id", "decision_id", "audit_id")
                        if hasattr(loaded, field_name)
                    ),
                    None,
                )
                if loaded_id != expected_id:
                    raise ReconciliationIntegrityError("existing object path has different content")
                return False
            self._atomic_write(path, payload)
            return True

    def _load(self, path: Path, model: type[BaseModel]) -> Any:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ReconciliationIntegrityError("reconciliation object is not a regular file")
            if metadata.st_size > self.max_object_bytes:
                raise ReconciliationIntegrityError("reconciliation object exceeds size limit")
            return model.model_validate_json(path.read_bytes())
        except ReconciliationIntegrityError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise ReconciliationIntegrityError(f"invalid reconciliation object: {exc}") from exc

    @staticmethod
    def _parse_id(value: str, prefix: str) -> str:
        expected_prefix = f"{prefix}:"
        if not value.startswith(expected_prefix):
            raise ReconciliationIntegrityError("invalid reconciliation object ID")
        digest = value[len(expected_prefix) :]
        if not _SAFE_FILE_ID.fullmatch(digest):
            raise ReconciliationIntegrityError("invalid reconciliation object ID")
        return digest

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.stem}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
