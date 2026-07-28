"""Strict, bounded presentation contracts for QE3 similarity results."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import ChannelWeights

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_SIMILARITY_RUN_ID_RE = re.compile(r"^similarity_run:[0-9a-f]{64}$")
_VISUALIZATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class _StrictPresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SimilarityVisualizationCandidate(_StrictPresentationModel):
    rank: int = Field(ge=1, le=50)
    symbol: str = Field(pattern=_SYMBOL_RE.pattern)
    combined_score: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    business_score: float | None = Field(default=None, ge=0.0, le=1.0)
    factor_score: float | None = Field(default=None, ge=0.0, le=1.0)
    price_volume_score: float | None = Field(default=None, ge=0.0, le=1.0)
    rank_stability: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=12)
    counterevidence: tuple[str, ...] = Field(min_length=1, max_length=12)

    @field_validator("evidence", "counterevidence")
    @classmethod
    def validate_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 1_000 for value in values):
            raise ValueError("evidence entries must contain 1 to 1000 characters")
        return values


class SimilarityVisualizationPayload(_StrictPresentationModel):
    schema_version: Literal[1] = 1
    visualization_id: str = Field(pattern=_VISUALIZATION_ID_RE.pattern)
    type: Literal["similarity_ranking"] = "similarity_ranking"
    similarity_run_id: str = Field(pattern=_SIMILARITY_RUN_ID_RE.pattern)
    similarity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_spec_id: str = Field(pattern=r"^research_spec:[0-9a-f]{64}$")
    target_symbols: tuple[str, ...] = Field(min_length=1, max_length=12)
    as_of: date
    candidate_universe: str = Field(min_length=1, max_length=128)
    weights: ChannelWeights
    candidates: tuple[SimilarityVisualizationCandidate, ...] = Field(min_length=1, max_length=50)
    excluded_symbol_count: int = Field(ge=0, le=100_000)

    @field_validator("target_symbols")
    @classmethod
    def validate_target_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(not _SYMBOL_RE.fullmatch(value) for value in values):
            raise ValueError("target_symbols must be unique canonical symbols")
        return values

    @model_validator(mode="after")
    def validate_identity_and_ranking(self) -> "SimilarityVisualizationPayload":
        if self.similarity_sha256 != self.similarity_run_id.split(":", 1)[1]:
            raise ValueError("similarity_sha256 must match similarity_run_id")
        ranks = tuple(candidate.rank for candidate in self.candidates)
        if ranks != tuple(range(1, len(self.candidates) + 1)):
            raise ValueError("candidate ranks must be contiguous and ordered")
        symbols = tuple(candidate.symbol for candidate in self.candidates)
        if len(set(symbols)) != len(symbols):
            raise ValueError("candidate symbols must be unique")
        return self


class SimilarityVisualizationSpec(_StrictPresentationModel):
    schema_version: Literal[1] = 1
    type: Literal["similarity_ranking"] = "similarity_ranking"
    visualization_id: str = Field(pattern=_VISUALIZATION_ID_RE.pattern)
    data_ref: str = Field(pattern=_VISUALIZATION_ID_RE.pattern)
    title: str = Field(min_length=1, max_length=200)
    similarity_run_id: str = Field(pattern=_SIMILARITY_RUN_ID_RE.pattern)
    similarity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_symbols: tuple[str, ...] = Field(min_length=1, max_length=12)
    as_of: date
    candidate_universe: str = Field(min_length=1, max_length=128)
    candidate_count: int = Field(ge=1, le=50)
    weights: ChannelWeights
    fallback_text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_refs(self) -> "SimilarityVisualizationSpec":
        if self.data_ref != self.visualization_id:
            raise ValueError("data_ref must match visualization_id")
        if self.similarity_sha256 != self.similarity_run_id.split(":", 1)[1]:
            raise ValueError("similarity_sha256 must match similarity_run_id")
        return self
