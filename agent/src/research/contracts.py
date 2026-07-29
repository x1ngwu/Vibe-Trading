"""Strict QE1 contracts for reproducible natural-language quant research.

The public payloads in this module are engine-neutral.  ``ResearchObject`` is
the persistence envelope shared by the future research store, API, and engine
adapters.  Its content identity intentionally excludes ``created_at`` while
including owner scope, parent references, and the complete payload, so retrying
the same semantic write is idempotent without losing audit timestamps.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, Mapping, Sequence, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OWNER_SCOPE = "household:v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_JSON_ADAPTER = TypeAdapter(Any)
_T = TypeVar("_T")

ObjectType = Literal[
    "research_spec",
    "data_snapshot_ref",
    "peer_set",
    "factor_evidence",
    "similarity_run",
    "strategy_spec",
    "engine_request",
    "backtest_run",
    "research_report",
]


class ContractError(ValueError):
    """Raised when a value cannot be represented by the canonical contract."""


class _StrictModel(BaseModel):
    """Frozen v1 model that rejects fields not declared by its schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON while rejecting non-finite numbers."""

    _reject_non_finite(value)
    if isinstance(value, BaseModel):
        normalized = value.model_dump(mode="json")
    else:
        normalized = _JSON_ADAPTER.dump_python(value, mode="json")
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value is not canonical JSON: {exc}") from exc


def canonical_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 of :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject_non_finite(value: Any) -> None:
    """Reject NaN/Infinity before serializers can coerce them to strings."""

    if isinstance(value, BaseModel):
        _reject_non_finite(value.model_dump(mode="python"))
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("value is not canonical JSON: non-finite float")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(key)
            _reject_non_finite(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_non_finite(item)


def _unique_tuple(values: Sequence[_T], field_name: str) -> tuple[_T, ...]:
    result = tuple(values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


class ObjectRef(_StrictModel):
    """Immutable, content-addressed reference to another research object."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    object_type: ObjectType
    object_id: str = Field(min_length=67, max_length=96)
    content_sha256: str = Field(pattern=_SHA256_RE.pattern)

    @model_validator(mode="after")
    def validate_identity(self) -> "ObjectRef":
        expected = f"{self.object_type}:{self.content_sha256}"
        if self.object_id != expected:
            raise ValueError("object_id must be object_type plus the full content_sha256")
        return self


class EngineIdentitySpec(_StrictModel):
    """Exact identity of an isolated external engine."""

    name: Literal["quantaxis", "vnpy"]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ResearchSpec(_StrictModel):
    """User-confirmable research intent before any data access or calculation."""

    object_type: Literal["research_spec"] = "research_spec"
    symbols: tuple[str, ...] = Field(min_length=1, max_length=12)
    as_of: date
    lookback_days: tuple[int, ...] = Field(min_length=1, max_length=8)
    candidate_universe: str = Field(min_length=1, max_length=128)
    peer_dimensions: tuple[
        Literal["industry", "market_cap", "liquidity", "listing_age"], ...
    ] = ("industry", "market_cap", "liquidity", "listing_age")
    feature_channels: tuple[Literal["business", "factor", "price_volume"], ...] = (
        "business",
        "factor",
        "price_volume",
    )
    requested_outputs: tuple[
        Literal["factor_evidence", "similarity", "strategy", "backtest", "report"], ...
    ] = ("factor_evidence", "similarity", "report")

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _unique_tuple(values, "symbols")
        if any(not _SYMBOL_RE.fullmatch(value) for value in values):
            raise ValueError("symbols contain an invalid canonical identifier")
        return values

    @field_validator("lookback_days")
    @classmethod
    def validate_lookbacks(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 or value > 10_000 for value in values):
            raise ValueError("lookback_days must be between 1 and 10000")
        return _unique_tuple(values, "lookback_days")

    @field_validator("peer_dimensions", "feature_channels", "requested_outputs")
    @classmethod
    def validate_unique_tokens(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_tuple(values, "contract tuple")


class DataSnapshotRef(_StrictModel):
    """Content-bound, point-in-time market-data snapshot description."""

    object_type: Literal["data_snapshot_ref"] = "data_snapshot_ref"
    snapshot_sha256: str = Field(pattern=_SHA256_RE.pattern)
    manifest_version: Literal["vibe.snapshot-manifest.v1"] = "vibe.snapshot-manifest.v1"
    as_of: date
    start_date: date
    end_date: date
    frequency: Literal["1d"] = "1d"
    adjustment: Literal["raw", "qfq", "hfq"]
    symbols: tuple[str, ...] = Field(min_length=1)
    fields: tuple[str, ...] = Field(min_length=1)
    requested_sources: tuple[str, ...] = Field(min_length=1)
    actual_sources: dict[str, str]
    anomalies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> "DataSnapshotRef":
        if self.start_date > self.end_date or self.end_date > self.as_of:
            raise ValueError("snapshot dates must satisfy start_date <= end_date <= as_of")
        symbols = _unique_tuple(self.symbols, "symbols")
        if symbols != self.symbols:
            raise ValueError("symbols must be stable")
        if set(self.actual_sources) - set(self.symbols):
            raise ValueError("actual_sources contains a symbol outside the snapshot")
        if set(self.actual_sources) != set(self.symbols):
            raise ValueError("actual_sources must disclose the source for every symbol")
        return self

    @field_validator("symbols")
    @classmethod
    def validate_snapshot_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _unique_tuple(values, "symbols")
        if any(not _SYMBOL_RE.fullmatch(value) for value in values):
            raise ValueError("symbols contain an invalid canonical identifier")
        return values

    @field_validator("fields", "requested_sources")
    @classmethod
    def validate_snapshot_tokens(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _unique_tuple(values, "snapshot tuple")
        if any(not _TOKEN_RE.fullmatch(value) for value in values):
            raise ValueError("snapshot tuple contains an invalid token")
        return values


class PeerSet(_StrictModel):
    """Rebuildable peer membership with explicit inclusion and exclusion reasons."""

    object_type: Literal["peer_set"] = "peer_set"
    research_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    target_symbol: str = Field(pattern=_SYMBOL_RE.pattern)
    members: tuple[str, ...] = Field(min_length=1)
    included_reasons: dict[str, tuple[str, ...]]
    excluded_reasons: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    coverage: float = Field(ge=0.0, le=1.0)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_membership(self) -> "PeerSet":
        if self.research_spec_ref.object_type != "research_spec":
            raise ValueError("research_spec_ref must reference research_spec")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref must reference data_snapshot_ref")
        members = _unique_tuple(self.members, "members")
        if self.target_symbol in members:
            raise ValueError("target_symbol must not be included in its own peer set")
        if set(self.included_reasons) != set(members):
            raise ValueError("included_reasons must cover every peer member exactly")
        return self


class FactorObservation(_StrictModel):
    """One target's factor value relative to its own peer set."""

    symbol: str = Field(pattern=_SYMBOL_RE.pattern)
    value: float
    peer_median: float
    robust_zscore: float
    percentile: float = Field(ge=0.0, le=1.0)
    source_fields: tuple[str, ...] = Field(min_length=1)


class FactorEvidence(_StrictModel):
    """Deterministic supporting and contradicting evidence for one factor."""

    object_type: Literal["factor_evidence"] = "factor_evidence"
    research_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    peer_set_refs: tuple[ObjectRef, ...] = Field(min_length=1)
    factor_id: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    direction: Literal["positive", "negative", "mixed"]
    observations: tuple[FactorObservation, ...] = Field(min_length=1)
    supporting_symbols: tuple[str, ...]
    contradicting_symbols: tuple[str, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    stability: float = Field(ge=0.0, le=1.0)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> "FactorEvidence":
        if self.research_spec_ref.object_type != "research_spec":
            raise ValueError("research_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        if any(ref.object_type != "peer_set" for ref in self.peer_set_refs):
            raise ValueError("peer_set_refs may only reference peer_set objects")
        observed = {item.symbol for item in self.observations}
        if (set(self.supporting_symbols) | set(self.contradicting_symbols)) - observed:
            raise ValueError("evidence symbol lists must be present in observations")
        if set(self.supporting_symbols) & set(self.contradicting_symbols):
            raise ValueError("a symbol cannot be both supporting and contradicting")
        return self


class ChannelWeights(_StrictModel):
    """Visible weighting for the three similarity channels."""

    business: float = Field(ge=0.0, le=1.0)
    factor: float = Field(ge=0.0, le=1.0)
    price_volume: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_sum(self) -> "ChannelWeights":
        if abs(self.business + self.factor + self.price_volume - 1.0) > 1e-12:
            raise ValueError("channel weights must sum to 1")
        return self


class StockCandidate(_StrictModel):
    """One explainable candidate in a similarity ranking."""

    symbol: str = Field(pattern=_SYMBOL_RE.pattern)
    rank: int = Field(ge=1)
    business_score: float | None = Field(default=None, ge=0.0, le=1.0)
    factor_score: float | None = Field(default=None, ge=0.0, le=1.0)
    price_volume_score: float | None = Field(default=None, ge=0.0, le=1.0)
    combined_score: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    evidence: tuple[str, ...] = Field(min_length=1)
    counterevidence: tuple[str, ...] = Field(min_length=1)


class SimilarityRun(_StrictModel):
    """Deterministic, explainable three-channel similarity result."""

    object_type: Literal["similarity_run"] = "similarity_run"
    research_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    factor_evidence_refs: tuple[ObjectRef, ...]
    weights: ChannelWeights
    candidates: tuple[StockCandidate, ...] = Field(min_length=1)
    excluded_symbols: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sensitivity_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_ranking(self) -> "SimilarityRun":
        if self.research_spec_ref.object_type != "research_spec":
            raise ValueError("research_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        if any(ref.object_type != "factor_evidence" for ref in self.factor_evidence_refs):
            raise ValueError("factor_evidence_refs contain the wrong object type")
        ranks = tuple(candidate.rank for candidate in self.candidates)
        if ranks != tuple(range(1, len(self.candidates) + 1)):
            raise ValueError("candidate ranks must be contiguous and ordered")
        symbols = tuple(candidate.symbol for candidate in self.candidates)
        _unique_tuple(symbols, "candidate symbols")
        return self


class SignalRule(_StrictModel):
    """One allowlisted, lag-aware deterministic signal condition."""

    field: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    operator: Literal["gt", "gte", "lt", "lte", "crosses_above", "crosses_below"]
    value: StrictFloat | StrictInt | str
    lookback_days: int = Field(ge=1, le=10_000)
    consecutive_days: int = Field(default=1, ge=1, le=1_000)


class RankingSpec(_StrictModel):
    """Cross-sectional ranking rule."""

    field: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,127}$")
    direction: Literal["ascending", "descending"]
    top_n: int = Field(ge=1, le=500)


class PortfolioSpec(_StrictModel):
    """Portfolio sizing and concentration limits."""

    weighting: Literal["equal"] = "equal"
    max_positions: int = Field(ge=1, le=500)
    max_position_weight: float = Field(gt=0.0, le=1.0)
    cash_buffer_weight: float = Field(default=0.0, ge=0.0, lt=1.0)


class ExecutionSpec(_StrictModel):
    """Auditable first-version execution assumptions."""

    signal_price: Literal["close"] = "close"
    fill_price: Literal["next_open", "next_vwap"] = "next_open"
    signal_lag_bars: int = Field(default=1, ge=1, le=10)
    rebalance: Literal["daily", "weekly", "monthly"]
    enforce_t_plus_one: bool = True
    board_lot: int = Field(default=100, ge=1)


class CostSpec(_StrictModel):
    """Versioned cost assumptions expressed without hidden defaults."""

    commission_bps: float = Field(ge=0.0, le=1_000.0)
    minimum_commission: float = Field(ge=0.0)
    sell_tax_bps: float = Field(ge=0.0, le=1_000.0)
    transfer_fee_bps: float = Field(ge=0.0, le=1_000.0)
    slippage_bps: float = Field(ge=0.0, le=1_000.0)
    rule_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RiskSpec(_StrictModel):
    """Basic portfolio risk limits for the first strategy family."""

    max_drawdown_stop: float | None = Field(default=None, gt=0.0, lt=1.0)
    max_turnover: float | None = Field(default=None, gt=0.0)


class EvaluationSpec(_StrictModel):
    """Explicit validation windows and reporting requirements."""

    train_end: date
    validation_end: date
    test_end: date
    benchmark: str = Field(min_length=1, max_length=128)
    walk_forward: bool = True

    @model_validator(mode="after")
    def validate_windows(self) -> "EvaluationSpec":
        if not self.train_end < self.validation_end < self.test_end:
            raise ValueError("evaluation dates must be strictly increasing")
        return self


class StrategySpec(_StrictModel):
    """Versioned, engine-neutral strategy definition."""

    object_type: Literal["strategy_spec"] = "strategy_spec"
    research_spec_ref: ObjectRef | None = None
    similarity_run_ref: ObjectRef | None = None
    data_snapshot_ref: ObjectRef
    title: str = Field(min_length=1, max_length=200)
    universe_symbols: tuple[str, ...] = Field(min_length=1, max_length=500)
    signals: tuple[SignalRule, ...] = Field(min_length=1, max_length=32)
    ranking: RankingSpec | None = None
    portfolio: PortfolioSpec
    execution: ExecutionSpec
    costs: CostSpec
    risk: RiskSpec
    evaluation: EvaluationSpec

    @model_validator(mode="after")
    def validate_refs(self) -> "StrategySpec":
        if self.research_spec_ref and self.research_spec_ref.object_type != "research_spec":
            raise ValueError("research_spec_ref has the wrong object type")
        if self.similarity_run_ref and self.similarity_run_ref.object_type != "similarity_run":
            raise ValueError("similarity_run_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        _unique_tuple(self.universe_symbols, "universe_symbols")
        if any(not _SYMBOL_RE.fullmatch(value) for value in self.universe_symbols):
            raise ValueError("universe_symbols contain an invalid canonical identifier")
        return self


class ResourceLimits(_StrictModel):
    """Hard resource budget passed to an isolated worker."""

    timeout_seconds: float = Field(gt=0.0, le=3_600.0)
    max_stdout_bytes: int = Field(ge=1_024, le=67_108_864)
    max_stderr_bytes: int = Field(ge=1_024, le=67_108_864)
    memory_bytes: int = Field(ge=67_108_864, le=8_589_934_592)


class EngineRequest(_StrictModel):
    """Compiled request shared by QUANTAXIS and vn.py adapters."""

    object_type: Literal["engine_request"] = "engine_request"
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    strategy_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    engine: EngineIdentitySpec
    operation: Literal["backtest", "normalize_ledger"]
    resource_limits: ResourceLimits
    random_seed: int = Field(ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_request_refs(self) -> "EngineRequest":
        if self.strategy_spec_ref.object_type != "strategy_spec":
            raise ValueError("strategy_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        return self


class BacktestMetrics(_StrictModel):
    """Small deterministic summary; complete ledgers remain artifacts."""

    total_return: float
    annualized_return: float | None = None
    max_drawdown: float
    turnover: float
    trade_count: int = Field(ge=0)


class BacktestRun(_StrictModel):
    """Normalized result of one exact engine request."""

    object_type: Literal["backtest_run"] = "backtest_run"
    engine_request_ref: ObjectRef
    strategy_spec_ref: ObjectRef
    data_snapshot_ref: ObjectRef
    engine: EngineIdentitySpec
    status: Literal["completed", "failed", "cancelled"]
    ledger_sha256: str | None = Field(default=None, pattern=_SHA256_RE.pattern)
    metrics: BacktestMetrics | None = None
    artifact_refs: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_run(self) -> "BacktestRun":
        if self.engine_request_ref.object_type != "engine_request":
            raise ValueError("engine_request_ref has the wrong object type")
        if self.strategy_spec_ref.object_type != "strategy_spec":
            raise ValueError("strategy_spec_ref has the wrong object type")
        if self.data_snapshot_ref.object_type != "data_snapshot_ref":
            raise ValueError("data_snapshot_ref has the wrong object type")
        if self.status == "completed" and (self.ledger_sha256 is None or self.metrics is None):
            raise ValueError("completed runs require a ledger hash and metrics")
        if self.status != "completed" and self.metrics is not None:
            raise ValueError("failed or cancelled runs cannot claim completed metrics")
        return self


class ResearchReport(_StrictModel):
    """Human-readable projection that only references deterministic objects."""

    object_type: Literal["research_report"] = "research_report"
    research_spec_ref: ObjectRef
    evidence_refs: tuple[ObjectRef, ...]
    similarity_run_ref: ObjectRef | None = None
    strategy_spec_refs: tuple[ObjectRef, ...] = ()
    backtest_run_refs: tuple[ObjectRef, ...] = ()
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=50_000)
    limitations: tuple[str, ...] = ()
    visualization_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_report_refs(self) -> "ResearchReport":
        if self.research_spec_ref.object_type != "research_spec":
            raise ValueError("research_spec_ref has the wrong object type")
        if any(ref.object_type != "factor_evidence" for ref in self.evidence_refs):
            raise ValueError("evidence_refs contain the wrong object type")
        if self.similarity_run_ref and self.similarity_run_ref.object_type != "similarity_run":
            raise ValueError("similarity_run_ref has the wrong object type")
        if any(ref.object_type != "strategy_spec" for ref in self.strategy_spec_refs):
            raise ValueError("strategy_spec_refs contain the wrong object type")
        if any(ref.object_type != "backtest_run" for ref in self.backtest_run_refs):
            raise ValueError("backtest_run_refs contain the wrong object type")
        return self


ResearchPayload = Annotated[
    ResearchSpec
    | DataSnapshotRef
    | PeerSet
    | FactorEvidence
    | SimilarityRun
    | StrategySpec
    | EngineRequest
    | BacktestRun
    | ResearchReport,
    Field(discriminator="object_type"),
]


def _normalize_parent_refs(parent_refs: Sequence[ObjectRef]) -> tuple[ObjectRef, ...]:
    refs = tuple(sorted(parent_refs, key=lambda ref: (ref.object_type, ref.object_id)))
    if len({ref.object_id for ref in refs}) != len(refs):
        raise ValueError("parent_refs must not contain duplicate object IDs")
    return refs


def _identity_material(
    *,
    object_type: ObjectType,
    owner_scope: str,
    parent_refs: Sequence[ObjectRef],
    payload: ResearchPayload,
) -> Mapping[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "object_type": object_type,
        "owner_scope": owner_scope,
        "parent_refs": [ref.model_dump(mode="json") for ref in _normalize_parent_refs(parent_refs)],
        "payload": payload.model_dump(mode="json"),
    }


def _collect_object_refs(value: Any) -> tuple[ObjectRef, ...]:
    """Recursively collect explicit references from one strict payload."""

    if isinstance(value, ObjectRef):
        return (value,)
    if isinstance(value, BaseModel):
        refs: list[ObjectRef] = []
        for field_name in value.__class__.model_fields:
            refs.extend(_collect_object_refs(getattr(value, field_name)))
        return tuple(refs)
    if isinstance(value, Mapping):
        refs = []
        for item in value.values():
            refs.extend(_collect_object_refs(item))
        return tuple(refs)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        refs = []
        for item in value:
            refs.extend(_collect_object_refs(item))
        return tuple(refs)
    return ()


class ResearchObject(_StrictModel):
    """Content-addressed persistence envelope for any QE1 public payload."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    object_type: ObjectType
    object_id: str = Field(min_length=67, max_length=96)
    owner_scope: str = Field(default=DEFAULT_OWNER_SCOPE, pattern=_SCOPE_RE.pattern)
    created_at: AwareDatetime
    parent_refs: tuple[ObjectRef, ...] = ()
    content_sha256: str = Field(pattern=_SHA256_RE.pattern)
    payload: ResearchPayload

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("parent_refs")
    @classmethod
    def normalize_parent_refs(cls, value: tuple[ObjectRef, ...]) -> tuple[ObjectRef, ...]:
        return _normalize_parent_refs(value)

    @model_validator(mode="after")
    def validate_content_identity(self) -> "ResearchObject":
        if self.payload.object_type != self.object_type:
            raise ValueError("object_type must match payload.object_type")
        explicit_refs = {ref.object_id for ref in _collect_object_refs(self.payload)}
        envelope_refs = {ref.object_id for ref in self.parent_refs}
        if not explicit_refs.issubset(envelope_refs):
            missing = sorted(explicit_refs - envelope_refs)
            raise ValueError(f"parent_refs omit payload references: {missing}")
        expected_hash = canonical_sha256(
            _identity_material(
                object_type=self.object_type,
                owner_scope=self.owner_scope,
                parent_refs=self.parent_refs,
                payload=self.payload,
            )
        )
        if self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match the canonical object content")
        if self.object_id != f"{self.object_type}:{expected_hash}":
            raise ValueError("object_id does not match the canonical object content")
        return self

    def ref(self) -> ObjectRef:
        """Return a strict reference to this object."""

        return ObjectRef(
            object_type=self.object_type,
            object_id=self.object_id,
            content_sha256=self.content_sha256,
        )


def create_research_object(
    payload: ResearchPayload,
    *,
    owner_scope: str = DEFAULT_OWNER_SCOPE,
    parent_refs: Sequence[ObjectRef] = (),
    created_at: datetime | None = None,
) -> ResearchObject:
    """Create and self-validate an idempotent content-addressed envelope."""

    normalized_refs = _normalize_parent_refs(parent_refs)
    digest = canonical_sha256(
        _identity_material(
            object_type=payload.object_type,
            owner_scope=owner_scope,
            parent_refs=normalized_refs,
            payload=payload,
        )
    )
    return ResearchObject(
        object_type=payload.object_type,
        object_id=f"{payload.object_type}:{digest}",
        owner_scope=owner_scope,
        created_at=created_at or datetime.now(timezone.utc),
        parent_refs=normalized_refs,
        content_sha256=digest,
        payload=payload,
    )
