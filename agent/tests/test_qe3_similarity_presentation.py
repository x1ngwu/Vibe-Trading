from __future__ import annotations

import json
import stat
from datetime import date, datetime, timezone

import pytest

from src.agent.context import _SYSTEM_PROMPT
from src.research.contracts import (
    ChannelWeights,
    DataSnapshotRef,
    ResearchSpec,
    SimilarityRun,
    StockCandidate,
    create_research_object,
)
from src.research.store import ResearchStore
from src.session.service import load_visualization_specs
from src.tools import build_registry, similarity_result_tool


def test_similarity_result_tool_is_registered_and_routed_without_shell_tools() -> None:
    registry = build_registry(include_shell_tools=False)

    assert "show_similarity_result" in registry.tool_names
    assert "bash" not in registry.tool_names
    assert "Call `show_similarity_result`" in _SYSTEM_PROMPT
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
                    evidence=("same_industry:白酒", "factor_distance:0.12"),
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
    spec = result["visualizations"][0]
    assert spec["type"] == "similarity_ranking"
    assert spec["similarity_run_id"] == similarity.object_id
    assert spec["target_symbols"] == ["600519.SH"]

    payload_path = run_dir / "artifacts" / "visualizations" / f"{spec['visualization_id']}.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["candidates"][0]["symbol"] == "000858.SZ"
    assert payload["candidates"][0]["rank_stability"] == 0.875
    assert payload["similarity_sha256"] == similarity.content_sha256
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "artifacts" / "visualizations.json").stat().st_mode) == 0o600
    assert load_visualization_specs(run_dir) == [spec]


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
