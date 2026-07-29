from __future__ import annotations

import json
import stat
from datetime import date, datetime, timezone

import pytest

from src.agent.context import _SYSTEM_PROMPT
from src.agent.loop import TOOL_RESULT_LIMIT
from src.research.contracts import (
    ChannelWeights,
    DataSnapshotRef,
    ResearchSpec,
    SimilarityRun,
    StockCandidate,
    create_research_object,
)
from src.research.similarity_presentation import SimilarityCandidateSummary
from src.research.store import ResearchStore
from src.session.models import Message
from src.session.service import SessionService, load_visualization_specs
from src.tools import build_registry, similarity_result_tool


def test_similarity_result_tool_is_registered_and_routed_without_shell_tools() -> None:
    registry = build_registry(include_shell_tools=False)

    assert "show_similarity_result" in registry.tool_names
    assert "bash" not in registry.tool_names
    assert "Call `show_similarity_result`" in _SYSTEM_PROMPT
    assert "use its bounded `candidate_summary`" in _SYSTEM_PROMPT
    assert "do not claim that its summarized candidates are unavailable" in _SYSTEM_PROMPT
    assert "candidate_summary_truncated" in _SYSTEM_PROMPT
    assert "<persisted-similarity-results>" in _SYSTEM_PROMPT
    assert similarity_result_tool.ShowSimilarityResultTool.requires_current_run_dir is True
    assert similarity_result_tool.ShowSimilarityResultTool.parameters["required"] == ["similarity_run_id"]


def _stored_similarity(tmp_path):
    store = ResearchStore(tmp_path / "research")
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH",),
            as_of=date(2026, 7, 25),
            lookback_days=(20, 60, 252),
            candidate_universe="csi300@2026-07-25",
        ),
        created_at=datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc),
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="9" * 64,
            as_of=date(2026, 7, 25),
            start_date=date(2025, 7, 25),
            end_date=date(2026, 7, 25),
            adjustment="qfq",
            symbols=("600519.SH", "000858.SZ", "600809.SH"),
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={
                "600519.SH": "fixture",
                "000858.SZ": "fixture",
                "600809.SH": "fixture",
            },
        ),
        parent_refs=(research.ref(),),
        created_at=datetime(2026, 7, 27, 1, 1, tzinfo=timezone.utc),
    )
    similarity = create_research_object(
        SimilarityRun(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            factor_evidence_refs=(),
            weights=ChannelWeights(business=0.3, factor=0.4, price_volume=0.3),
            candidates=(
                StockCandidate(
                    rank=1,
                    symbol="000858.SZ",
                    business_score=0.91,
                    factor_score=0.82,
                    price_volume_score=0.73,
                    combined_score=0.82,
                    coverage=1.0,
                    evidence=(
                        "same_industry:白酒",
                        "factor_distance:0.12",
                        "detail:" + "x" * 400,
                        "not_in_model_summary",
                    ),
                    counterevidence=("market_cap_gap:0.31",),
                ),
                StockCandidate(
                    rank=2,
                    symbol="600809.SH",
                    business_score=0.83,
                    factor_score=0.72,
                    price_volume_score=0.66,
                    combined_score=0.74,
                    coverage=0.9,
                    evidence=("same_industry:白酒",),
                    counterevidence=("liquidity_gap:0.22",),
                ),
            ),
            excluded_symbols={"600519.SH": ("target_symbol",)},
            sensitivity_notes=(
                "sensitivity_summary:000858.SZ:scenarios=4;mean_rank_stability=0.875000",
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=datetime(2026, 7, 27, 1, 2, tzinfo=timezone.utc),
    )
    store.put(research)
    store.put(snapshot)
    store.put(similarity)
    return store, similarity


def test_similarity_result_tool_persists_bounded_payload_and_manifest(monkeypatch, tmp_path) -> None:
    store, similarity = _stored_similarity(tmp_path)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(similarity_result_tool, "safe_run_dir", lambda _value: run_dir)

    result = json.loads(
        similarity_result_tool.ShowSimilarityResultTool(store).execute(
            similarity_run_id=similarity.object_id,
            top_n=1,
            title="贵州茅台相似标的",
            run_dir="injected-by-agent-loop",
        )
    )

    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    assert result["candidate_summary_count"] == 1
    assert result["candidate_summary_truncated"] is False
    assert len(result["candidate_summary"]) == 1
    summary = result["candidate_summary"][0]
    assert summary["rank"] == 1
    assert summary["symbol"] == "000858.SZ"
    assert summary["combined_score"] == 0.82
    assert summary["coverage"] == 1.0
    assert summary["business_score"] == 0.91
    assert summary["factor_score"] == 0.82
    assert summary["price_volume_score"] == 0.73
    assert summary["rank_stability"] == 0.875
    assert summary["missing_channels"] == []
    assert summary["evidence"] == ["same_industry:白酒"]
    assert summary["counterevidence"] == ["market_cap_gap:0.31"]
    spec = result["visualizations"][0]
    assert spec["type"] == "similarity_ranking"
    assert spec["similarity_run_id"] == similarity.object_id
    assert spec["target_symbols"] == ["600519.SH"]

    payload_path = run_dir / "artifacts" / "visualizations" / f"{spec['visualization_id']}.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["candidates"][0]["symbol"] == "000858.SZ"
    assert payload["candidates"][0]["rank_stability"] == 0.875
    assert len(payload["candidates"][0]["evidence"]) == 4
    assert len(payload["candidates"][0]["evidence"]) > len(summary["evidence"])
    assert payload["similarity_sha256"] == similarity.content_sha256
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "artifacts" / "visualizations.json").stat().st_mode) == 0o600
    assert load_visualization_specs(run_dir) == [spec]


def test_candidate_summary_contract_fails_closed_for_inconsistent_or_unbounded_data() -> None:
    valid = {
        "rank": 1,
        "symbol": "000858.SZ",
        "combined_score": 0.82,
        "coverage": 0.7,
        "business_score": 0.91,
        "factor_score": 0.82,
        "price_volume_score": None,
        "rank_stability": 0.875,
        "missing_channels": ("price_volume",),
        "evidence": ("same_industry:白酒",),
        "counterevidence": ("price_volume_channel:unavailable",),
    }
    assert SimilarityCandidateSummary.model_validate(valid).missing_channels == ("price_volume",)

    with pytest.raises(ValueError, match="must match unavailable"):
        SimilarityCandidateSummary.model_validate({**valid, "missing_channels": ()})
    with pytest.raises(ValueError, match="1 to 100 characters"):
        SimilarityCandidateSummary.model_validate({**valid, "evidence": ("x" * 101,)})
    with pytest.raises(ValueError):
        SimilarityCandidateSummary.model_validate(
            {**valid, "counterevidence": ("one", "two")}
        )


def test_similarity_result_tool_caps_model_summary_before_agent_loop_limit(
    monkeypatch,
    tmp_path,
) -> None:
    store, similarity = _stored_similarity(tmp_path)
    base = SimilarityRun.model_validate(similarity.payload)
    candidates = tuple(
        StockCandidate(
            rank=index,
            symbol=f"600{index:03d}.SH",
            business_score=round(0.99 - index / 100, 6),
            factor_score=round(0.98 - index / 100, 6),
            price_volume_score=round(0.97 - index / 100, 6),
            combined_score=round(0.98 - index / 100, 6),
            coverage=1.0,
            evidence=("e" * 1_000,),
            counterevidence=("c" * 1_000,),
        )
        for index in range(1, 13)
    )
    expanded = create_research_object(
        base.model_copy(update={"candidates": candidates}),
        parent_refs=similarity.parent_refs,
        created_at=datetime(2026, 7, 27, 1, 3, tzinfo=timezone.utc),
    )
    store.put(expanded)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(similarity_result_tool, "safe_run_dir", lambda _value: run_dir)

    raw_result = similarity_result_tool.ShowSimilarityResultTool(store).execute(
        similarity_run_id=expanded.object_id,
        top_n=12,
        title="T" * 200,
        run_dir="injected-by-agent-loop",
    )
    result = json.loads(raw_result)

    assert result["candidate_count"] == 12
    assert result["candidate_summary_count"] == 10
    assert result["candidate_summary_truncated"] is True
    assert len(result["candidate_summary"]) == 10
    assert all(len(item["evidence"]) == 1 for item in result["candidate_summary"])
    assert all(len(item["evidence"][0]) == 100 for item in result["candidate_summary"])
    assert len(raw_result) <= similarity_result_tool._MODEL_RESULT_MAX_CHARS < TOOL_RESULT_LIMIT


def test_similarity_result_tool_checks_model_limit_before_writing(
    monkeypatch,
    tmp_path,
) -> None:
    store, similarity = _stored_similarity(tmp_path)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(similarity_result_tool, "safe_run_dir", lambda _value: run_dir)
    monkeypatch.setattr(similarity_result_tool, "_MODEL_RESULT_MAX_CHARS", 1)

    with pytest.raises(ValueError, match="model context limit"):
        similarity_result_tool.ShowSimilarityResultTool(store).execute(
            similarity_run_id=similarity.object_id,
            run_dir="injected-by-agent-loop",
        )
    assert not run_dir.exists()


def test_similarity_result_tool_fails_closed_for_unknown_object(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    monkeypatch.setattr(similarity_result_tool, "safe_run_dir", lambda _value: run_dir)
    store = ResearchStore(tmp_path / "research")
    with pytest.raises(ValueError, match="not found"):
        similarity_result_tool.ShowSimilarityResultTool(store).execute(
            similarity_run_id="similarity_run:" + "a" * 64,
            run_dir="injected-by-agent-loop",
        )
    assert not run_dir.exists()


def test_similarity_manifest_rejects_extra_fields(tmp_path) -> None:
    store, similarity = _stored_similarity(tmp_path)
    run_dir = tmp_path / "run"
    similarity_result_tool.safe_run_dir = lambda _value: run_dir
    result = json.loads(
        similarity_result_tool.ShowSimilarityResultTool(store).execute(
            similarity_run_id=similarity.object_id,
            run_dir="injected-by-agent-loop",
        )
    )
    spec = result["visualizations"][0]
    spec["secret"] = "must-not-reach-history"
    manifest = run_dir / "artifacts" / "visualizations.json"
    manifest.write_text(json.dumps([spec]), encoding="utf-8")
    assert load_visualization_specs(run_dir) == []

    history = SessionService._convert_messages_to_history(
        [
            Message(
                role="assistant",
                content="已有相似结果。",
                metadata={"visualizations": [spec]},
            ),
            Message(role="user", content="继续比较。"),
        ]
    )
    assert len(history) == 1
    assert "<persisted-similarity-results>" not in history[0]["content"]
    assert similarity.object_id not in history[0]["content"]
