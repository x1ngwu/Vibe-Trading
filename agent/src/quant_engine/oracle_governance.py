"""QE6-5 observable upgrade, rollback, and historical-oracle replay audits."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import canonical_sha256

from .backtest_run import NormalizedBacktestRecord
from .reconciliation import (
    ReconciliationArtifact,
    ReconciliationArtifactRef,
    ReconciliationEngineIdentity,
    ReconciliationIntegrityError,
    ReconciliationStore,
)


ORACLE_REPLAY_AUDIT_SCHEMA = "vibe.oracle-replay-audit.v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_AUDIT_ID_PATTERN = r"^oracle-replay-audit:[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HistoricalRunIdentity(_StrictModel):
    """The immutable old BacktestRun identities required for replay."""

    run_id: str = Field(pattern=r"^backtest-record:[0-9a-f]{64}$")
    run_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    owner_scope: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    strategy_stream_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    strategy_version_id: str = Field(pattern=r"^strategy-version:[0-9a-f]{64}$")
    engine_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    backtest_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    original_engine: ReconciliationEngineIdentity


class OracleReplayAudit(_StrictModel):
    """Content-addressed evidence for one release transition or old-run replay."""

    schema_version: Literal["vibe.oracle-replay-audit.v1"] = ORACLE_REPLAY_AUDIT_SCHEMA
    audit_id: str = Field(pattern=_AUDIT_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    mode: Literal["upgrade", "rollback", "historical_replay"]
    historical_run: HistoricalRunIdentity
    previous_oracle: ReconciliationEngineIdentity | None = None
    expected_oracle: ReconciliationEngineIdentity
    observed_oracle: ReconciliationEngineIdentity | None = None
    status: Literal["matched", "diverged", "blocked", "not_available"]
    code: Literal[
        "INDEPENDENT_ORACLE_MATCH",
        "FIRST_DIVERGENCE",
        "ENGINE_RELEASE_MISMATCH",
        "ARTIFACT_LINEAGE_MISMATCH",
        "CAPABILITY_NOT_AVAILABLE",
    ]
    reconciliation_ref: ReconciliationArtifactRef | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_audit(self) -> "OracleReplayAudit":
        if self.historical_run.original_engine.name != "quantaxis":
            raise ValueError("historical replay requires a QE5 QUANTAXIS run")
        releases = tuple(
            release
            for release in (
                self.previous_oracle,
                self.expected_oracle,
                self.observed_oracle,
            )
            if release is not None
        )
        if any(release.name != "vnpy" for release in releases):
            raise ValueError("oracle replay releases must all be vnpy")
        if self.mode in {"upgrade", "rollback"}:
            if self.previous_oracle is None or self.previous_oracle == self.expected_oracle:
                raise ValueError("release transition requires two different oracle releases")
        elif self.previous_oracle is not None:
            raise ValueError("historical replay does not have a release transition")
        if self.status == "not_available":
            if (
                self.code != "CAPABILITY_NOT_AVAILABLE"
                or self.observed_oracle is not None
                or self.reconciliation_ref is not None
            ):
                raise ValueError("not_available audit cannot claim execution evidence")
        elif self.observed_oracle is None:
            raise ValueError("executed replay audit requires observed_oracle")
        if self.status in {"matched", "diverged"} and self.reconciliation_ref is None:
            raise ValueError("completed replay audit requires reconciliation evidence")
        expected_codes = {
            "matched": "INDEPENDENT_ORACLE_MATCH",
            "diverged": "FIRST_DIVERGENCE",
            "not_available": "CAPABILITY_NOT_AVAILABLE",
        }
        if self.status in expected_codes and self.code != expected_codes[self.status]:
            raise ValueError("replay status and code disagree")
        if self.status == "blocked" and self.code not in {
            "ENGINE_RELEASE_MISMATCH",
            "ARTIFACT_LINEAGE_MISMATCH",
        }:
            raise ValueError("blocked replay requires a fail-closed diagnostic")
        expected = canonical_sha256(oracle_replay_audit_material(self))
        if self.content_sha256 != expected:
            raise ValueError("audit content_sha256 does not match content")
        if self.audit_id != f"oracle-replay-audit:{expected}":
            raise ValueError("audit_id does not match content")
        return self


def oracle_replay_audit_material(audit: OracleReplayAudit) -> Mapping[str, Any]:
    return audit.model_dump(
        mode="json",
        exclude={"audit_id", "content_sha256", "created_at"},
    )


def historical_run_identity(record: NormalizedBacktestRecord) -> HistoricalRunIdentity:
    """Extract replay lineage without consulting mutable current configuration."""

    provenance = record.provenance
    return HistoricalRunIdentity(
        run_id=record.run_id,
        run_content_sha256=record.content_sha256,
        owner_scope=record.owner_scope,
        strategy_stream_id=provenance.strategy_stream_id,
        strategy_version_id=provenance.strategy_version_id,
        engine_request_sha256=provenance.engine_request_ref.content_sha256,
        execution_plan_sha256=provenance.execution_plan_sha256,
        snapshot_sha256=provenance.snapshot_sha256,
        backtest_input_sha256=provenance.backtest_input_sha256,
        ledger_sha256=record.ledger_sha256,
        original_engine=ReconciliationEngineIdentity(
            name=provenance.engine.name,
            version=provenance.engine_version,
            commit=provenance.engine.commit,
            source_sha256=provenance.engine_source_sha256,
        ),
    )


def _artifact_matches_historical_run(
    artifact: ReconciliationArtifact,
    historical: HistoricalRunIdentity,
    observed_oracle: ReconciliationEngineIdentity,
) -> bool:
    return (
        artifact.owner_scope == historical.owner_scope
        and artifact.stream_id == historical.strategy_stream_id
        and artifact.strategy_version_id == historical.strategy_version_id
        and artifact.engine_request_sha256 == historical.engine_request_sha256
        and artifact.execution_plan_sha256 == historical.execution_plan_sha256
        and artifact.snapshot_sha256 == historical.snapshot_sha256
        and artifact.qe5_backtest_input_sha256 == historical.backtest_input_sha256
        and artifact.qe5_ledger_sha256 == historical.ledger_sha256
        and artifact.qe5_engine == historical.original_engine
        and artifact.vnpy_engine == observed_oracle
    )


def audit_oracle_replay(
    record: NormalizedBacktestRecord,
    *,
    mode: Literal["upgrade", "rollback", "historical_replay"],
    expected_oracle: ReconciliationEngineIdentity,
    previous_oracle: ReconciliationEngineIdentity | None = None,
    observed_oracle: ReconciliationEngineIdentity | None = None,
    reconciliation: ReconciliationArtifact | None = None,
    capability_available: bool = True,
    created_at: datetime | None = None,
) -> OracleReplayAudit:
    """Derive an observable replay result without silently accepting drift."""

    historical = historical_run_identity(record)
    return _audit_oracle_replay_identity(
        historical,
        mode=mode,
        expected_oracle=expected_oracle,
        previous_oracle=previous_oracle,
        observed_oracle=observed_oracle,
        reconciliation=reconciliation,
        capability_available=capability_available,
        created_at=created_at,
    )


def _audit_oracle_replay_identity(
    historical: HistoricalRunIdentity,
    *,
    mode: Literal["upgrade", "rollback", "historical_replay"],
    expected_oracle: ReconciliationEngineIdentity,
    previous_oracle: ReconciliationEngineIdentity | None,
    observed_oracle: ReconciliationEngineIdentity | None,
    reconciliation: ReconciliationArtifact | None,
    capability_available: bool,
    created_at: datetime | None,
) -> OracleReplayAudit:
    if not capability_available:
        status: Literal["matched", "diverged", "blocked", "not_available"] = (
            "not_available"
        )
        code: Literal[
            "INDEPENDENT_ORACLE_MATCH",
            "FIRST_DIVERGENCE",
            "ENGINE_RELEASE_MISMATCH",
            "ARTIFACT_LINEAGE_MISMATCH",
            "CAPABILITY_NOT_AVAILABLE",
        ] = "CAPABILITY_NOT_AVAILABLE"
        observed = None
        reference = None
    elif observed_oracle != expected_oracle:
        status = "blocked"
        code = "ENGINE_RELEASE_MISMATCH"
        observed = observed_oracle
        reference = None
    elif reconciliation is None or not _artifact_matches_historical_run(
        reconciliation,
        historical,
        observed_oracle,
    ):
        status = "blocked"
        code = "ARTIFACT_LINEAGE_MISMATCH"
        observed = observed_oracle
        reference = reconciliation.ref() if reconciliation is not None else None
    else:
        status = reconciliation.comparison_status
        code = (
            "INDEPENDENT_ORACLE_MATCH"
            if status == "matched"
            else "FIRST_DIVERGENCE"
        )
        observed = observed_oracle
        reference = reconciliation.ref()
    material = {
        "schema_version": ORACLE_REPLAY_AUDIT_SCHEMA,
        "mode": mode,
        "historical_run": historical.model_dump(mode="json"),
        "previous_oracle": (
            previous_oracle.model_dump(mode="json")
            if previous_oracle is not None
            else None
        ),
        "expected_oracle": expected_oracle.model_dump(mode="json"),
        "observed_oracle": observed.model_dump(mode="json") if observed is not None else None,
        "status": status,
        "code": code,
        "reconciliation_ref": (
            reference.model_dump(mode="json") if reference is not None else None
        ),
    }
    digest = canonical_sha256(material)
    return OracleReplayAudit(
        audit_id=f"oracle-replay-audit:{digest}",
        content_sha256=digest,
        created_at=created_at or datetime.now(timezone.utc),
        **material,
    )


class OracleGovernanceStore(ReconciliationStore):
    """Reconciliation store extended with immutable replay audit records."""

    def __init__(self, root: Path, *, max_object_bytes: int = 4_194_304) -> None:
        super().__init__(root, max_object_bytes=max_object_bytes)
        self.audits_dir = self.root / "audits"
        if self.audits_dir.is_symlink():
            raise ReconciliationIntegrityError("oracle audit directory must not be a symlink")
        self.audits_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.audits_dir, 0o700)

    def put_audit(
        self,
        audit: OracleReplayAudit,
        *,
        record: NormalizedBacktestRecord,
    ) -> bool:
        if historical_run_identity(record) != audit.historical_run:
            raise ReconciliationIntegrityError("replay audit does not bind the supplied old run")
        artifact = None
        if audit.reconciliation_ref is not None:
            artifact = self.get_artifact(
                audit.reconciliation_ref.artifact_id,
                owner_scope=audit.historical_run.owner_scope,
            )
            if artifact is None or artifact.ref() != audit.reconciliation_ref:
                raise ReconciliationIntegrityError("replay audit references a missing artifact")
        expected = _audit_oracle_replay_identity(
            audit.historical_run,
            mode=audit.mode,
            previous_oracle=audit.previous_oracle,
            expected_oracle=audit.expected_oracle,
            observed_oracle=audit.observed_oracle,
            reconciliation=artifact,
            capability_available=audit.status != "not_available",
            created_at=audit.created_at,
        )
        if expected != audit:
            raise ReconciliationIntegrityError("replay audit was not derived from its evidence")
        return self._put(
            self.audits_dir / f"{audit.content_sha256}.json",
            audit,
            expected_id=audit.audit_id,
        )

    def get_audit(
        self,
        audit_id: str,
        *,
        owner_scope: str,
        record: NormalizedBacktestRecord | None = None,
    ) -> OracleReplayAudit | None:
        digest = self._parse_id(audit_id, "oracle-replay-audit")
        path = self.audits_dir / f"{digest}.json"
        if not path.exists() and not path.is_symlink():
            return None
        audit = self._load(path, OracleReplayAudit)
        if audit.audit_id != audit_id or audit.content_sha256 != digest:
            raise ReconciliationIntegrityError("replay audit identity does not match its path")
        if audit.historical_run.owner_scope != owner_scope:
            return None
        if record is not None and historical_run_identity(record) != audit.historical_run:
            raise ReconciliationIntegrityError("stored replay audit does not bind the old run")
        artifact = None
        if audit.reconciliation_ref is not None:
            artifact = self.get_artifact(
                audit.reconciliation_ref.artifact_id,
                owner_scope=owner_scope,
            )
            if artifact is None or artifact.ref() != audit.reconciliation_ref:
                raise ReconciliationIntegrityError("stored replay audit artifact is missing")
        expected = _audit_oracle_replay_identity(
            audit.historical_run,
            mode=audit.mode,
            previous_oracle=audit.previous_oracle,
            expected_oracle=audit.expected_oracle,
            observed_oracle=audit.observed_oracle,
            reconciliation=artifact,
            capability_available=audit.status != "not_available",
            created_at=audit.created_at,
        )
        if expected != audit:
            raise ReconciliationIntegrityError("stored replay audit is inconsistent")
        return audit
