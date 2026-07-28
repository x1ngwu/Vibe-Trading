"""QE3 E2E-01: persisted similarity result through tool, SSE, and history."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

from src.research.contracts import (
    ChannelWeights,
    DataSnapshotRef,
    ResearchSpec,
    SimilarityRun,
    StockCandidate,
    create_research_object,
)
from src.research.store import ResearchStore
from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus
from src.session.service import SessionService
from src.session.store import SessionStore
from src.tools import similarity_result_tool


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


def _seed_two_sample_similarity(tmp_path):
    targets = ("600519.SH", "000858.SZ")
    candidates = (
        "000568.SZ",
        "600809.SH",
        "600600.SH",
        "600887.SH",
        "000895.SZ",
        "002304.SZ",
        "000799.SZ",
        "000596.SZ",
        "603369.SH",
        "600132.SH",
    )
    store = ResearchStore(tmp_path / "research")
    created_at = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
    research = create_research_object(
        ResearchSpec(
            symbols=targets,
            as_of=date(2026, 7, 27),
            lookback_days=(20, 60, 252),
            candidate_universe="csi300@2026-07-27",
        ),
        created_at=created_at,
    )
    snapshot_symbols = (*targets, *candidates)
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="9" * 64,
            as_of=date(2026, 7, 27),
            start_date=date(2025, 7, 24),
            end_date=date(2026, 7, 27),
            adjustment="qfq",
            symbols=snapshot_symbols,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("baostock",),
            actual_sources={symbol: "baostock" for symbol in snapshot_symbols},
        ),
        parent_refs=(research.ref(),),
        created_at=created_at,
    )
    ranked = tuple(
        StockCandidate(
            rank=index,
            symbol=symbol,
            business_score=round(0.95 - index * 0.025, 6),
            factor_score=round(0.94 - index * 0.03, 6),
            price_volume_score=None if index == 10 else round(0.93 - index * 0.035, 6),
            combined_score=round(0.94 - index * 0.03, 6),
            coverage=0.7 if index == 10 else 1.0,
            evidence=(f"shared_business_profile:{symbol}", f"factor_distance:{index / 100:.2f}"),
            counterevidence=(
                (
                    "price_volume_channel:unavailable"
                    if index == 10
                    else f"liquidity_gap:{index / 50:.2f}"
                ),
            ),
        )
        for index, symbol in enumerate(candidates, start=1)
    )
    similarity = create_research_object(
        SimilarityRun(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            factor_evidence_refs=(),
            weights=ChannelWeights(business=0.3, factor=0.4, price_volume=0.3),
            candidates=ranked,
            excluded_symbols={target: ("target_symbol",) for target in targets},
            sensitivity_notes=tuple(
                f"sensitivity_summary:{symbol}:scenarios=4;mean_rank_stability={1 - index / 100:.6f}"
                for index, symbol in enumerate(candidates, start=1)
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=created_at,
    )
    store.put(research)
    store.put(snapshot)
    store.put(similarity)
    return store, similarity


def test_two_samples_to_ten_candidates_survives_sse_and_history(
    monkeypatch,
    tmp_path,
) -> None:
    research_store, similarity = _seed_two_sample_similarity(tmp_path)
    run_dir = tmp_path / "runs" / "qe3-e2e-01"
    monkeypatch.setattr(similarity_result_tool, "safe_run_dir", lambda _value: run_dir)
    tool_result = json.loads(
        similarity_result_tool.ShowSimilarityResultTool(research_store).execute(
            similarity_run_id=similarity.object_id,
            top_n=10,
            title="Two-stock similarity candidates",
            run_dir="injected-by-agent-loop",
        )
    )
    expected_spec = tool_result["visualizations"][0]

    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    session_store = SessionStore(tmp_path / "sessions")
    event_bus = EventBus()
    service = SessionService(session_store, event_bus, tmp_path / "runs")
    session = service.create_session("QE3 E2E-01")
    attempt = Attempt(session_id=session.session_id, prompt="展示这两个样本的十只相似股")
    session_store.create_attempt(attempt)

    async def _completed_agent(*args, **kwargs):
        del args, kwargs
        return {
            "status": "success",
            "content": "已生成十只相似候选，并保留正反证、覆盖率与敏感性。",
            "run_dir": str(run_dir),
        }

    monkeypatch.setattr(service, "_run_with_agent", _completed_agent)
    asyncio.run(service._run_attempt(session, attempt))

    persisted_attempt = session_store.get_attempt(session.session_id, attempt.attempt_id)
    assert persisted_attempt is not None
    assert persisted_attempt.status == AttemptStatus.COMPLETED

    completion_events = [
        event
        for event in event_bus.replay(session.session_id, replay_all=True)
        if event.event_type == "attempt.completed"
    ]
    assert len(completion_events) == 1
    assert completion_events[0].data["visualizations"] == [expected_spec]

    assistant_messages = [
        message for message in session_store.get_messages(session.session_id)
        if message.role == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].metadata["visualizations"] == [expected_spec]
    assert assistant_messages[0].metadata["run_id"] == "qe3-e2e-01"

    payload = json.loads(
        (run_dir / "artifacts" / "visualizations" / f"{expected_spec['visualization_id']}.json")
        .read_text(encoding="utf-8")
    )
    assert len(payload["target_symbols"]) == 2
    assert len(payload["candidates"]) == 10
    assert all(candidate["evidence"] for candidate in payload["candidates"])
    assert all(candidate["counterevidence"] for candidate in payload["candidates"])
    assert payload["candidates"][-1]["coverage"] == 0.7
    assert payload["candidates"][-1]["price_volume_score"] is None
