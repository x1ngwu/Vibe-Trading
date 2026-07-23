"""QE2 strict metadata envelope for existing market-data loaders.

This module is an adapter layer: existing ``loader.fetch`` return values stay
unchanged while QE2 callers can opt into complete per-symbol provenance,
capability gates, deterministic duplicate handling, and content-bound cache
identity.  No function in this module performs network access directly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.research.contracts import DataSnapshotRef, canonical_json, canonical_sha256

DATA_ENVELOPE_SCHEMA = "vibe.data-envelope.v1"

InstrumentType = Literal["stock", "etf", "index"]
Adjustment = Literal["raw", "qfq", "hfq"]
OutcomeStatus = Literal["ok", "not_available"]
AnomalyKind = Literal[
    "duplicate_same",
    "empty_result",
    "provider_error",
    "source_unavailable",
    "unsupported_capability",
]


class DataEnvelopeError(ValueError):
    """Base error for fail-closed QE2 envelope construction."""


class DuplicateConflictError(DataEnvelopeError):
    """Raised when duplicate symbol/date rows disagree in any requested field."""


class IncompleteDataError(DataEnvelopeError):
    """Raised when a downstream consumer requires every requested symbol."""


class UnitConflictError(DataEnvelopeError):
    """Raised when fallbacks disagree on units within one instrument type."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoaderCapability(_StrictModel):
    """Versioned facts a loader explicitly promises for this adapter."""

    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    instrument_types: tuple[InstrumentType, ...]
    intervals: tuple[str, ...]
    adjustments: tuple[Adjustment, ...]
    fields: tuple[str, ...]
    field_units: dict[InstrumentType, dict[str, str]]

    def supports(self, request: "DataFetchRequest", symbol: str) -> bool:
        instrument_type = request.instrument_types[symbol]
        units = self.field_units.get(instrument_type, {})
        return (
            instrument_type in self.instrument_types
            and request.interval in self.intervals
            and request.adjustment in self.adjustments
            and set(request.fields) <= set(self.fields)
            and set(request.fields) <= set(units)
        )


class DataFetchRequest(_StrictModel):
    """One immutable multi-symbol request before any provider is called."""

    schema_version: Literal["vibe.data-request.v1"] = "vibe.data-request.v1"
    symbols: tuple[str, ...] = Field(min_length=1, max_length=128)
    instrument_types: dict[str, InstrumentType]
    start_date: date
    end_date: date
    interval: Literal["1D"] = "1D"
    adjustment: Adjustment
    fields: tuple[str, ...] = Field(min_length=1)
    requested_sources: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_request(self) -> "DataFetchRequest":
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        if set(self.instrument_types) != set(self.symbols):
            raise ValueError("instrument_types must cover every requested symbol exactly")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("fields must not contain duplicates")
        if len(set(self.requested_sources)) != len(self.requested_sources):
            raise ValueError("requested_sources must not contain duplicates")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class DataAnomaly(_StrictModel):
    kind: AnomalyKind
    symbol: str
    source: str
    trade_date: date | None = None
    fields: tuple[str, ...] = ()
    detail: str


class SymbolOutcome(_StrictModel):
    symbol: str
    status: OutcomeStatus
    attempted_sources: tuple[str, ...]
    actual_source: str | None
    row_count: int = Field(ge=0)


class DataEnvelopeManifest(_StrictModel):
    """Serializable provenance and content identity for runtime frames."""

    schema_version: Literal["vibe.data-envelope.v1"] = DATA_ENVELOPE_SCHEMA
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbols: tuple[str, ...]
    instrument_types: dict[str, InstrumentType]
    start_date: date
    end_date: date
    interval: Literal["1D"]
    adjustment: Adjustment
    fields: tuple[str, ...]
    requested_sources: tuple[str, ...]
    source_versions: dict[str, str]
    actual_sources: dict[str, str]
    units: dict[str, dict[str, str]]
    outcomes: tuple[SymbolOutcome, ...]
    anomalies: tuple[DataAnomaly, ...]

    @model_validator(mode="after")
    def validate_coverage(self) -> "DataEnvelopeManifest":
        expected = set(self.symbols)
        if set(self.actual_sources) != expected or set(self.units) != expected:
            raise ValueError("actual_sources and units must cover every requested symbol")
        if set(self.source_versions) != set(self.requested_sources):
            raise ValueError("source_versions must cover every requested source")
        if {item.symbol for item in self.outcomes} != expected:
            raise ValueError("outcomes must cover every requested symbol")
        return self


class OfflineDataSnapshot(_StrictModel):
    """Canonical JSON payload whose exact bytes are the snapshot content."""

    schema_version: Literal["vibe.offline-data-snapshot.v1"] = "vibe.offline-data-snapshot.v1"
    request: DataFetchRequest
    source_versions: dict[str, str]
    actual_sources: dict[str, str]
    units: dict[str, dict[str, str]]
    outcomes: tuple[SymbolOutcome, ...]
    anomalies: tuple[DataAnomaly, ...]
    bars: dict[str, tuple[dict[str, Any], ...]]

    @model_validator(mode="after")
    def validate_snapshot(self) -> "OfflineDataSnapshot":
        request = self.request
        expected_symbols = set(request.symbols)
        if set(self.source_versions) != set(request.requested_sources):
            raise ValueError("offline snapshot source_versions do not cover requested sources")
        if set(self.actual_sources) != expected_symbols or set(self.units) != expected_symbols:
            raise ValueError("offline snapshot provenance does not cover requested symbols")
        if {item.symbol for item in self.outcomes} != expected_symbols:
            raise ValueError("offline snapshot outcomes do not cover requested symbols")
        successful = {item.symbol for item in self.outcomes if item.status == "ok"}
        if set(self.bars) != successful:
            raise ValueError("offline snapshot bars must match successful symbol outcomes")
        expected_keys = {"trade_date", *request.fields}
        for symbol, rows in self.bars.items():
            if len(rows) != next(item.row_count for item in self.outcomes if item.symbol == symbol):
                raise ValueError(f"offline snapshot row_count mismatch for {symbol}")
            if any(set(row) != expected_keys for row in rows):
                raise ValueError(f"offline snapshot row fields mismatch for {symbol}")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True)
class DataEnvelope:
    """Runtime frames plus their strict, JSON-serializable manifest."""

    request: DataFetchRequest
    manifest: DataEnvelopeManifest
    frames: dict[str, pd.DataFrame]

    def require_complete(self) -> "DataEnvelope":
        missing = tuple(item.symbol for item in self.manifest.outcomes if item.status != "ok")
        if missing:
            raise IncompleteDataError(f"data envelope is incomplete: symbols={list(missing)}")
        return self

    def snapshot_ref(self) -> DataSnapshotRef:
        """Return the public contract bound to the canonical artifact bytes."""

        self.require_complete()
        anomaly_tokens = tuple(
            f"{item.kind}:{item.symbol}:{item.source}"
            for item in self.manifest.anomalies
        )
        return DataSnapshotRef(
            snapshot_sha256=self.manifest.snapshot_sha256,
            as_of=self.request.end_date,
            start_date=self.request.start_date,
            end_date=self.request.end_date,
            adjustment=self.request.adjustment,
            symbols=self.request.symbols,
            fields=self.request.fields,
            requested_sources=self.request.requested_sources,
            actual_sources=self.manifest.actual_sources,
            anomalies=anomaly_tokens,
        )


def fetch_data_envelope(
    request: DataFetchRequest,
    *,
    loaders: Mapping[str, Any],
    capabilities: Mapping[str, LoaderCapability],
) -> DataEnvelope:
    """Fetch each symbol through an explicit fallback chain without shrinking it."""

    frames: dict[str, pd.DataFrame] = {}
    outcomes: list[SymbolOutcome] = []
    anomalies: list[DataAnomaly] = []
    actual_sources: dict[str, str] = {}
    units: dict[str, dict[str, str]] = {}
    source_versions = {
        source: capabilities[source].version if source in capabilities else "undeclared"
        for source in request.requested_sources
    }

    for symbol in request.symbols:
        attempted: list[str] = []
        selected_source: str | None = None
        selected_frame: pd.DataFrame | None = None
        selected_units: dict[str, str] | None = None

        for source in request.requested_sources:
            capability = capabilities.get(source)
            if capability is None or capability.source != source or not capability.supports(request, symbol):
                anomalies.append(
                    DataAnomaly(
                        kind="unsupported_capability",
                        symbol=symbol,
                        source=source,
                        detail="source does not declare the requested instrument/interval/adjustment/fields",
                    )
                )
                continue
            attempted.append(source)
            loader = loaders.get(source)
            if loader is None or getattr(loader, "name", None) != source:
                anomalies.append(
                    DataAnomaly(
                        kind="source_unavailable",
                        symbol=symbol,
                        source=source,
                        detail="loader instance is unavailable",
                    )
                )
                continue
            try:
                if hasattr(loader, "is_available") and not loader.is_available():
                    raise _SourceUnavailable("loader reported unavailable")
                fetched = loader.fetch(
                    [symbol],
                    request.start_date.isoformat(),
                    request.end_date.isoformat(),
                    fields=None,
                    interval=request.interval,
                )
            except _SourceUnavailable as exc:
                anomalies.append(
                    DataAnomaly(
                        kind="source_unavailable",
                        symbol=symbol,
                        source=source,
                        detail=str(exc),
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - provider failure is explicit provenance
                anomalies.append(
                    DataAnomaly(
                        kind="provider_error",
                        symbol=symbol,
                        source=source,
                        detail=f"provider raised {type(exc).__name__}",
                    )
                )
                continue

            frame = fetched.get(symbol) if isinstance(fetched, Mapping) else None
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                anomalies.append(
                    DataAnomaly(
                        kind="empty_result",
                        symbol=symbol,
                        source=source,
                        detail="provider returned no rows for the requested symbol",
                    )
                )
                continue
            normalized, duplicate_anomalies = normalize_symbol_frame(
                frame,
                symbol=symbol,
                source=source,
                fields=request.fields,
            )
            anomalies.extend(duplicate_anomalies)
            selected_source = source
            selected_frame = normalized
            selected_units = {
                field: capability.field_units[request.instrument_types[symbol]][field]
                for field in request.fields
            }
            break

        if selected_source is None or selected_frame is None or selected_units is None:
            actual_sources[symbol] = "not_available"
            units[symbol] = {}
            outcomes.append(
                SymbolOutcome(
                    symbol=symbol,
                    status="not_available",
                    attempted_sources=tuple(attempted),
                    actual_source=None,
                    row_count=0,
                )
            )
            continue

        frames[symbol] = selected_frame
        actual_sources[symbol] = selected_source
        units[symbol] = selected_units
        outcomes.append(
            SymbolOutcome(
                symbol=symbol,
                status="ok",
                attempted_sources=tuple(attempted),
                actual_source=selected_source,
                row_count=len(selected_frame),
            )
        )

    bars = {
        symbol: _canonical_frame_rows(frame, fields=request.fields)
        for symbol, frame in sorted(frames.items())
    }
    _validate_uniform_units(request, units)
    artifact = OfflineDataSnapshot(
        request=request,
        source_versions=source_versions,
        actual_sources=actual_sources,
        units=units,
        outcomes=tuple(outcomes),
        anomalies=tuple(anomalies),
        bars=bars,
    )
    manifest = DataEnvelopeManifest(
        request_sha256=request.request_sha256,
        snapshot_sha256=artifact.snapshot_sha256,
        symbols=request.symbols,
        instrument_types=request.instrument_types,
        start_date=request.start_date,
        end_date=request.end_date,
        interval=request.interval,
        adjustment=request.adjustment,
        fields=request.fields,
        requested_sources=request.requested_sources,
        source_versions=source_versions,
        actual_sources=actual_sources,
        units=units,
        outcomes=tuple(outcomes),
        anomalies=tuple(anomalies),
    )
    return DataEnvelope(request=request, manifest=manifest, frames=frames)


def write_offline_snapshot(envelope: DataEnvelope, path: Path) -> OfflineDataSnapshot:
    """Atomically persist a canonical snapshot that can be replayed without a loader."""

    target = Path(path)
    if _has_symlink_component(target):
        raise DataEnvelopeError("offline snapshot path must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    bars = {
        symbol: _canonical_frame_rows(frame, fields=envelope.request.fields)
        for symbol, frame in sorted(envelope.frames.items())
    }
    artifact = OfflineDataSnapshot(
        request=envelope.request,
        source_versions=envelope.manifest.source_versions,
        actual_sources=envelope.manifest.actual_sources,
        units=envelope.manifest.units,
        outcomes=envelope.manifest.outcomes,
        anomalies=envelope.manifest.anomalies,
        bars=bars,
    )
    if artifact.snapshot_sha256 != envelope.manifest.snapshot_sha256:
        raise DataEnvelopeError("runtime frames no longer match the envelope snapshot hash")
    payload = canonical_json(artifact).encode("utf-8")
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
        try:
            os.link(temporary_path, target)
        except FileExistsError:
            if _has_symlink_component(target) or not target.is_file():
                raise DataEnvelopeError("existing offline snapshot must be a regular file")
            if hashlib.sha256(target.read_bytes()).hexdigest() != artifact.snapshot_sha256:
                raise DataEnvelopeError("existing offline snapshot has different content")
        directory_descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return artifact


def read_offline_snapshot(path: Path, *, expected_sha256: str) -> DataEnvelope:
    """Load a content-bound local snapshot without constructing any loader."""

    source = Path(path)
    if _has_symlink_component(source) or not source.is_file():
        raise DataEnvelopeError("offline snapshot must be a regular, non-symlink file")
    payload = source.read_bytes()
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise DataEnvelopeError("offline snapshot content does not match expected_sha256")
    artifact = OfflineDataSnapshot.model_validate_json(payload)
    frames: dict[str, pd.DataFrame] = {}
    for symbol, rows in artifact.bars.items():
        frame = pd.DataFrame(rows)
        frame.index = pd.to_datetime(frame.pop("trade_date"))
        frame.index.name = "trade_date"
        frames[symbol] = frame.loc[:, list(artifact.request.fields)]
    request = artifact.request
    manifest = DataEnvelopeManifest(
        request_sha256=request.request_sha256,
        snapshot_sha256=actual_sha256,
        symbols=request.symbols,
        instrument_types=request.instrument_types,
        start_date=request.start_date,
        end_date=request.end_date,
        interval=request.interval,
        adjustment=request.adjustment,
        fields=request.fields,
        requested_sources=request.requested_sources,
        source_versions=artifact.source_versions,
        actual_sources=artifact.actual_sources,
        units=artifact.units,
        outcomes=artifact.outcomes,
        anomalies=artifact.anomalies,
    )
    return DataEnvelope(request=request, manifest=manifest, frames=frames)


def normalize_symbol_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    fields: tuple[str, ...],
) -> tuple[pd.DataFrame, tuple[DataAnomaly, ...]]:
    """Sort dates, collapse identical duplicates, and reject conflicting ones."""

    missing_fields = sorted(set(fields) - set(frame.columns))
    if missing_fields:
        raise DataEnvelopeError(
            f"{source} frame for {symbol} is missing requested fields: {missing_fields}"
        )
    normalized = frame.loc[:, list(fields)].copy()
    try:
        normalized.index = pd.to_datetime(normalized.index)
    except Exception as exc:  # noqa: BLE001 - normalize provider index failure
        raise DataEnvelopeError(f"{source} frame for {symbol} has an invalid date index") from exc
    normalized.index.name = "trade_date"
    normalized = normalized.sort_index(kind="stable")
    if {"open", "high", "low", "close"} <= set(fields):
        invalid = (
            (normalized["high"] < normalized[["open", "close", "low"]].max(axis=1))
            | (normalized["low"] > normalized[["open", "close", "high"]].min(axis=1))
            | (normalized[["open", "high", "low", "close"]] <= 0).any(axis=1)
        )
        if invalid.any():
            first_date = pd.Timestamp(normalized.index[invalid][0]).date().isoformat()
            raise DataEnvelopeError(
                f"{source} frame for {symbol} violates OHLC invariants on {first_date}"
            )
    anomalies: list[DataAnomaly] = []
    keep_positions: list[int] = []
    for trade_date, positions in _duplicate_groups(normalized.index).items():
        rows = normalized.iloc[positions]
        first = rows.iloc[0]
        conflicting_fields = tuple(
            field for field in fields if not _series_values_equal(rows[field], first[field])
        )
        if conflicting_fields:
            raise DuplicateConflictError(
                f"conflicting duplicate for {symbol} on {trade_date.date().isoformat()}: "
                f"fields={list(conflicting_fields)}"
            )
        keep_positions.append(positions[0])
        if len(positions) > 1:
            anomalies.append(
                DataAnomaly(
                    kind="duplicate_same",
                    symbol=symbol,
                    source=source,
                    trade_date=trade_date.date(),
                    fields=fields,
                    detail=f"collapsed {len(positions)} identical rows to one",
                )
            )
    return normalized.iloc[keep_positions], tuple(anomalies)


def make_downstream_cache_key(
    *,
    snapshot_sha256: str,
    consumer: str,
    parameters: Mapping[str, Any],
) -> str:
    """Bind any derived cache entry to exact snapshot content and parameters."""

    if len(snapshot_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in snapshot_sha256):
        raise DataEnvelopeError("snapshot_sha256 must be a lowercase SHA-256")
    if not consumer:
        raise DataEnvelopeError("consumer must not be empty")
    return canonical_sha256(
        {
            "version": "vibe.downstream-cache.v1",
            "snapshot_sha256": snapshot_sha256,
            "consumer": consumer,
            "parameters": dict(parameters),
        }
    )


class _SourceUnavailable(RuntimeError):
    pass


def _duplicate_groups(index: pd.DatetimeIndex) -> dict[pd.Timestamp, list[int]]:
    groups: dict[pd.Timestamp, list[int]] = {}
    for position, value in enumerate(index):
        groups.setdefault(pd.Timestamp(value), []).append(position)
    return groups


def _series_values_equal(series: pd.Series, first: Any) -> bool:
    return bool(((series == first) | (series.isna() & pd.isna(first))).all())


def _canonical_frame_rows(
    frame: pd.DataFrame,
    *,
    fields: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for position, trade_date in enumerate(frame.index):
        record: dict[str, Any] = {"trade_date": pd.Timestamp(trade_date).isoformat()}
        for field in fields:
            value = frame[field].iloc[position]
            if pd.isna(value):
                record[field] = None
            elif hasattr(value, "item"):
                record[field] = value.item()
            else:
                record[field] = value
        rows.append(record)
    return tuple(rows)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path)
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _validate_uniform_units(
    request: DataFetchRequest,
    units: Mapping[str, Mapping[str, str]],
) -> None:
    observed: dict[tuple[str, str], set[str]] = {}
    for symbol, field_units in units.items():
        instrument_type = request.instrument_types[symbol]
        for field, unit in field_units.items():
            observed.setdefault((instrument_type, field), set()).add(unit)
    conflicts = {
        f"{instrument_type}.{field}": sorted(values)
        for (instrument_type, field), values in observed.items()
        if len(values) > 1
    }
    if conflicts:
        raise UnitConflictError(f"fallback unit conflict: {conflicts}")
