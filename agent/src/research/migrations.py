"""Explicit, fixture-backed migrations for persisted research contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from .contracts import ResearchObject, ResearchSpec, canonical_sha256, create_research_object


class MigrationError(ValueError):
    """Raised when no exact, audited schema migration is available."""


@dataclass(frozen=True)
class MigrationResult:
    """Auditable result of one deterministic migration."""

    source_version: str
    target_version: str
    source_sha256: str
    object: ResearchObject


_AWARE_DATETIME = TypeAdapter(AwareDatetime)


def migrate_research_object(raw: Mapping[str, Any]) -> MigrationResult:
    """Migrate one supported legacy envelope to schema 1.0, failing closed."""

    source = dict(raw)
    version = source.get("schema_version")
    if version == "1.0":
        try:
            current = ResearchObject.model_validate(source)
        except ValidationError as exc:
            raise MigrationError(f"invalid schema 1.0 object: {exc}") from exc
        return MigrationResult("1.0", "1.0", canonical_sha256(source), current)
    if version != "0.9":
        raise MigrationError(f"unsupported schema migration: {version!r} -> '1.0'")
    return _migrate_research_spec_0_9(source)


def _migrate_research_spec_0_9(source: Mapping[str, Any]) -> MigrationResult:
    expected_envelope = {"schema_version", "type", "owner", "created_at", "payload"}
    if set(source) != expected_envelope:
        raise MigrationError(_key_error("legacy envelope", source, expected_envelope))
    if source["type"] != "research":
        raise MigrationError("schema 0.9 migration only supports type='research'")
    owner = source["owner"]
    if not isinstance(owner, str):
        raise MigrationError("legacy owner must be a string")
    payload = source["payload"]
    if not isinstance(payload, Mapping):
        raise MigrationError("legacy payload must be an object")
    expected_payload = {"symbols", "as_of", "lookback", "universe"}
    if set(payload) != expected_payload:
        raise MigrationError(_key_error("legacy payload", payload, expected_payload))
    lookback = payload["lookback"]
    if not isinstance(lookback, int) or isinstance(lookback, bool):
        raise MigrationError("legacy lookback must be an integer")
    try:
        created_at: datetime = _AWARE_DATETIME.validate_python(source["created_at"])
        research_spec = ResearchSpec(
            symbols=payload["symbols"],
            as_of=payload["as_of"],
            lookback_days=(lookback,),
            candidate_universe=payload["universe"],
        )
        migrated = create_research_object(
            research_spec,
            owner_scope=owner,
            created_at=created_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise MigrationError(f"invalid schema 0.9 research object: {exc}") from exc
    return MigrationResult("0.9", "1.0", canonical_sha256(source), migrated)


def _key_error(where: str, value: Mapping[str, Any], expected: set[str]) -> str:
    actual = set(value)
    return (
        f"{where} keys mismatch; unknown={sorted(actual - expected)}, "
        f"missing={sorted(expected - actual)}"
    )
