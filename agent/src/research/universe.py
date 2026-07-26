"""Point-in-time universe snapshots and deterministic QE3 peer-set construction.

The legacy factor-bench universe loader selects the latest constituents it can
fetch and may fall back to a hand-picked list. That behaviour is useful for an
exploratory bench, but it is not admissible for reproducible similarity
research. This module consumes only a fixed local history, filters facts by
their ``known_at`` timestamp, and binds every generated ``PeerSet`` to the
resulting universe snapshot hash.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    DataSnapshotRef,
    PeerSet,
    ResearchObject,
    ResearchSpec,
    canonical_sha256,
    create_research_object,
)

UNIVERSE_HISTORY_SCHEMA = "vibe.universe-history.v1"
UNIVERSE_SNAPSHOT_SCHEMA = "vibe.universe-snapshot.v1"


class UniverseError(ValueError):
    """Raised when a universe cannot be safely reconstructed."""


class _StrictUniverseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseInstrument(_StrictUniverseModel):
    """Versioned security identity and lifecycle metadata."""

    security_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._:-]{0,63}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    instrument_type: Literal["stock", "etf", "index"]
    listing_date: date
    delisting_date: date | None = None
    primary_listing: bool = True
    known_at: AwareDatetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "UniverseInstrument":
        if self.delisting_date is not None and self.delisting_date < self.listing_date:
            raise ValueError("delisting_date must not precede listing_date")
        return self


class UniverseMembership(_StrictUniverseModel):
    """One inclusive constituent-membership interval."""

    source_row_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
    effective_from: date
    effective_to: date | None = None
    known_at: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self) -> "UniverseMembership":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("membership effective_to must not precede effective_from")
        return self

    def active_on(self, as_of: date) -> bool:
        return self.effective_from <= as_of and (
            self.effective_to is None or as_of <= self.effective_to
        )


class UniverseSnapshot(_StrictUniverseModel):
    """Canonical subset of universe facts visible at one end-of-day cutoff."""

    schema_version: Literal["vibe.universe-snapshot.v1"] = UNIVERSE_SNAPSHOT_SCHEMA
    universe_id: Literal["csi300"]
    index_symbol: Literal["000300.SH"]
    as_of: date
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source_version: str = Field(min_length=1, max_length=128)
    cutoff_timezone: Literal["Asia/Shanghai"]
    instruments: tuple[UniverseInstrument, ...]
    memberships: tuple[UniverseMembership, ...]

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def active_symbols(self) -> tuple[str, ...]:
        return tuple(
            item.symbol for item in self.memberships if item.active_on(self.as_of)
        )


class PointInTimeUniverse(_StrictUniverseModel):
    """Offline history from which a past constituent set can be rebuilt."""

    schema_version: Literal["vibe.universe-history.v1"] = UNIVERSE_HISTORY_SCHEMA
    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    universe_id: Literal["csi300"]
    index_symbol: Literal["000300.SH"]
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source_version: str = Field(min_length=1, max_length=128)
    cutoff_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    instruments: tuple[UniverseInstrument, ...] = Field(min_length=1)
    memberships: tuple[UniverseMembership, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_history(self) -> "PointInTimeUniverse":
        symbols = [item.symbol for item in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("universe instruments must have unique symbols")
        declared = set(symbols)
        if {item.symbol for item in self.memberships} - declared:
            raise ValueError("universe memberships reference undeclared symbols")

        row_ids = [item.source_row_id for item in self.memberships]
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("universe membership source_row_id values must be unique")

        by_security: dict[str, list[UniverseInstrument]] = defaultdict(list)
        for instrument in self.instruments:
            by_security[instrument.security_id].append(instrument)
        for security_id, listings in by_security.items():
            primary_count = sum(item.primary_listing for item in listings)
            if primary_count != 1:
                raise ValueError(
                    f"security_id {security_id} must have exactly one primary listing"
                )

        by_symbol: dict[str, list[UniverseMembership]] = defaultdict(list)
        for membership in self.memberships:
            by_symbol[membership.symbol].append(membership)
        for symbol, periods in by_symbol.items():
            ordered = sorted(
                periods,
                key=lambda item: (
                    item.effective_from,
                    item.effective_to or date.max,
                    item.source_row_id,
                ),
            )
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if previous.effective_to is None or current.effective_from <= previous.effective_to:
                    raise ValueError(f"membership periods overlap for {symbol}")
        return self

    @property
    def history_sha256(self) -> str:
        return canonical_sha256(self)

    def snapshot(self, as_of: date) -> UniverseSnapshot:
        """Materialize only facts known by end-of-day ``as_of`` in Shanghai."""

        timezone = ZoneInfo(self.cutoff_timezone)

        def visible(known_at: datetime) -> bool:
            return known_at.astimezone(timezone).date() <= as_of

        instruments = tuple(
            sorted(
                (item for item in self.instruments if visible(item.known_at)),
                key=lambda item: (item.symbol, item.security_id),
            )
        )
        memberships = tuple(
            sorted(
                (item for item in self.memberships if visible(item.known_at)),
                key=lambda item: (
                    item.symbol,
                    item.effective_from,
                    item.effective_to or date.max,
                    item.source_row_id,
                ),
            )
        )
        return UniverseSnapshot(
            universe_id=self.universe_id,
            index_symbol=self.index_symbol,
            as_of=as_of,
            source=self.source,
            source_version=self.source_version,
            cutoff_timezone=self.cutoff_timezone,
            instruments=instruments,
            memberships=memberships,
        )


def _has_symlink_component(path: Path) -> bool:
    candidate = path.absolute()
    return any(part.is_symlink() for part in (candidate, *candidate.parents))


def load_point_in_time_universe(path: Path) -> PointInTimeUniverse:
    """Load a strict local universe history without network access."""

    fixture_path = Path(path)
    if _has_symlink_component(fixture_path) or not fixture_path.is_file():
        raise UniverseError("universe history must be a regular, non-symlink file")
    try:
        return PointInTimeUniverse.model_validate_json(
            fixture_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise UniverseError(f"invalid universe history: {exc}") from exc


def _require_research_inputs(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    *,
    target_symbol: str,
    universe: PointInTimeUniverse,
) -> tuple[ResearchSpec, DataSnapshotRef]:
    if research.object_type != "research_spec" or not isinstance(
        research.payload, ResearchSpec
    ):
        raise UniverseError("research must be a research_spec object")
    if data_snapshot.object_type != "data_snapshot_ref" or not isinstance(
        data_snapshot.payload, DataSnapshotRef
    ):
        raise UniverseError("data_snapshot must be a data_snapshot_ref object")
    if research.owner_scope != data_snapshot.owner_scope:
        raise UniverseError("research and data snapshot cross owner_scope boundaries")
    if research.ref() not in data_snapshot.parent_refs:
        raise UniverseError("data_snapshot does not descend from the research_spec")

    spec = research.payload
    snapshot_ref = data_snapshot.payload
    expected_universe = f"{universe.universe_id}@{spec.as_of.isoformat()}"
    if spec.candidate_universe != expected_universe:
        raise UniverseError(
            f"candidate_universe must be the fixed point-in-time key {expected_universe}"
        )
    if snapshot_ref.as_of != spec.as_of or snapshot_ref.end_date != spec.as_of:
        raise UniverseError("research and data snapshot must share the exact as_of date")
    if target_symbol not in spec.symbols:
        raise UniverseError("target_symbol is not one of the research symbols")
    if target_symbol not in snapshot_ref.symbols:
        raise UniverseError("target_symbol is absent from the fixed data snapshot")
    return spec, snapshot_ref


def build_peer_set(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    universe: PointInTimeUniverse,
    *,
    target_symbol: str,
) -> PeerSet:
    """Build an ordered, explainable CSI300 peer set for SM-01/SM-02."""

    spec, snapshot_ref = _require_research_inputs(
        research,
        data_snapshot,
        target_symbol=target_symbol,
        universe=universe,
    )
    universe_snapshot = universe.snapshot(spec.as_of)
    instruments = {item.symbol: item for item in universe_snapshot.instruments}
    active_symbols = set(universe_snapshot.active_symbols)
    visible_membership_symbols = {item.symbol for item in universe_snapshot.memberships}
    symbols = sorted(set(instruments) | visible_membership_symbols)
    snapshot_symbols = set(snapshot_ref.symbols)
    primary_symbols = {
        item.security_id: item.symbol
        for item in universe_snapshot.instruments
        if item.primary_listing
    }

    members: list[str] = []
    included_reasons: dict[str, tuple[str, ...]] = {}
    excluded_reasons: dict[str, tuple[str, ...]] = {}
    eligible_security_ids: set[str] = set()
    covered_security_ids: set[str] = set()

    for symbol in symbols:
        if symbol == target_symbol:
            excluded_reasons[symbol] = ("target_symbol",)
            continue
        if symbol not in active_symbols:
            excluded_reasons[symbol] = (
                f"not_index_member_at_as_of:{spec.as_of.isoformat()}",
            )
            continue

        instrument = instruments.get(symbol)
        if instrument is None:
            excluded_reasons[symbol] = ("instrument_metadata_not_visible_at_as_of",)
            continue
        if instrument.instrument_type != "stock":
            excluded_reasons[symbol] = (
                f"instrument_type_not_stock:{instrument.instrument_type}",
            )
            continue
        if instrument.listing_date > spec.as_of:
            excluded_reasons[symbol] = (
                f"not_listed_at_as_of:{instrument.listing_date.isoformat()}",
            )
            continue
        if instrument.delisting_date is not None and instrument.delisting_date <= spec.as_of:
            excluded_reasons[symbol] = (
                f"delisted_at_as_of:{instrument.delisting_date.isoformat()}",
            )
            continue

        primary_symbol = primary_symbols.get(instrument.security_id)
        if primary_symbol is None:
            excluded_reasons[symbol] = ("primary_security_metadata_not_visible_at_as_of",)
            continue
        if symbol != primary_symbol:
            excluded_reasons[symbol] = (f"duplicate_security:{primary_symbol}",)
            continue

        eligible_security_ids.add(instrument.security_id)
        if symbol not in snapshot_symbols:
            excluded_reasons[symbol] = ("missing_from_data_snapshot",)
            continue

        covered_security_ids.add(instrument.security_id)
        members.append(symbol)
        included_reasons[symbol] = (
            f"index_member:{universe.universe_id}@{spec.as_of.isoformat()}",
            f"security_id:{instrument.security_id}",
            f"listed_since:{instrument.listing_date.isoformat()}",
            f"data_snapshot_sha256:{snapshot_ref.snapshot_sha256}",
            f"universe_snapshot_sha256:{universe_snapshot.snapshot_sha256}",
        )

    if not members:
        raise UniverseError("fixed universe produced no eligible peers with snapshot data")

    coverage = (
        len(covered_security_ids) / len(eligible_security_ids)
        if eligible_security_ids
        else 1.0
    )
    warnings = [
        f"universe_snapshot_sha256:{universe_snapshot.snapshot_sha256}",
        f"universe_source:{universe_snapshot.source}@{universe_snapshot.source_version}",
    ]
    if coverage < 1.0:
        warnings.append(
            f"eligible_data_coverage:{len(covered_security_ids)}/{len(eligible_security_ids)}"
        )

    return PeerSet(
        research_spec_ref=research.ref(),
        data_snapshot_ref=data_snapshot.ref(),
        target_symbol=target_symbol,
        members=tuple(members),
        included_reasons=included_reasons,
        excluded_reasons=excluded_reasons,
        coverage=coverage,
        warnings=tuple(warnings),
    )


def build_peer_set_object(
    research: ResearchObject,
    data_snapshot: ResearchObject,
    universe: PointInTimeUniverse,
    *,
    target_symbol: str,
    created_at: datetime | None = None,
) -> ResearchObject:
    """Create the persisted, content-addressed object for a rebuilt peer set."""

    payload = build_peer_set(
        research,
        data_snapshot,
        universe,
        target_symbol=target_symbol,
    )
    return create_research_object(
        payload,
        owner_scope=research.owner_scope,
        parent_refs=(research.ref(), data_snapshot.ref()),
        created_at=created_at,
    )
