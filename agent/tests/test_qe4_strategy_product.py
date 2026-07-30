"""QE4-5 Agent/API/Session visualization product integration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

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
from src.session.models import Message
from src.session.service import (
    SessionService,
    _persisted_strategy_history,
    load_visualization_specs,
)
from src.strategy_spec.presentation import (
    StrategyConfirmationVisualizationPayload,
)
from src.strategy_spec.version_store import StrategyVersionStore
from src.tools import build_registry
from src.tools.strategy_draft_tool import ConfirmStrategyTool, DraftStrategyTool

_T0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
_SYMBOLS = ("600519.SH", "000858.SZ", "000001.SZ", "600036.SH")


def _seed_store(tmp_path: Path):
    store = ResearchStore(tmp_path / "research")
    research = create_research_object(
        ResearchSpec(
            symbols=("600519.SH",),
            as_of=date(2025, 6, 30),
            lookback_days=(20, 60),
            candidate_universe="qe4-product-fixture",
            requested_outputs=("similarity", "strategy", "backtest"),
        ),
        created_at=_T0,
    )
    snapshot = create_research_object(
        DataSnapshotRef(
            snapshot_sha256="6" * 64,
            as_of=date(2025, 6, 30),
            start_date=date(2023, 1, 3),
            end_date=date(2025, 6, 30),
            adjustment="qfq",
            symbols=_SYMBOLS,
            fields=("open", "high", "low", "close", "volume", "amount"),
            requested_sources=("fixture",),
            actual_sources={symbol: "fixture" for symbol in _SYMBOLS},
        ),
        parent_refs=(research.ref(),),
        created_at=_T0,
    )
    similarity = create_research_object(
        SimilarityRun(
            research_spec_ref=research.ref(),
            data_snapshot_ref=snapshot.ref(),
            factor_evidence_refs=(),
            weights=ChannelWeights(business=0.3, factor=0.4, price_volume=0.3),
            candidates=tuple(
                StockCandidate(
                    symbol=symbol,
                    rank=index,
                    business_score=0.8,
                    factor_score=0.7,
                    price_volume_score=0.6,
                    combined_score=0.7,
                    coverage=1.0,
                    evidence=(f"candidate-{index}",),
                    counterevidence=(f"risk-{index}",),
                )
                for index, symbol in enumerate(_SYMBOLS[1:], start=1)
            ),
        ),
        parent_refs=(research.ref(), snapshot.ref()),
        created_at=_T0,
    )
    for item in (research, snapshot, similarity):
        store.put(item)
    return store, research, snapshot, similarity


def _proposal(top_n: int = 3) -> dict:
    return {
        "schema_version": "vibe.strategy-draft.v1",
        "template_id": "top_n_rebalance",
        "title": "低波动月度策略",
        "ranking_field": "volatility_20d",
        "ranking_direction": "ascending",
        "top_n": top_n,
        "rebalance": "monthly",
        "risk_level": "low",
    }


def test_strategy_tools_are_session_injected_and_never_offer_worker_execution() -> None:
    registry = build_registry(session_id="session-product", include_shell_tools=False)

    assert "draft_strategy" in registry.tool_names
    assert "confirm_strategy" in registry.tool_names
    assert isinstance(registry.get("draft_strategy"), DraftStrategyTool)
    assert "never starts a backtest worker" in DraftStrategyTool.description
    assert "does not start a backtest worker" in ConfirmStrategyTool.description
    assert "Call `draft_strategy`" in _SYSTEM_PROMPT
    assert "<persisted-strategy-version>" in _SYSTEM_PROMPT
    assert "Never treat Enter" in _SYSTEM_PROMPT


def test_direct_and_similarity_sources_share_card_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    research_store, research, snapshot, similarity = _seed_store(tmp_path)
    version_db = tmp_path / "versions.db"
    run_direct = tmp_path / "runs" / "direct"
    run_similarity = tmp_path / "runs" / "similarity"
    monkeypatch.setattr(
        "src.tools.strategy_draft_tool.safe_run_dir",
        lambda value: run_direct if value == "direct-run" else run_similarity,
    )
    direct = json.loads(
        DraftStrategyTool(
            default_session_id="session-direct",
            research_store=research_store,
            version_db_path=version_db,
        ).execute(
            instruction="在这四只股票里每月选波动最低的三只，风险低一些",
            proposal=_proposal(),
            research_spec_id=research.object_id,
            data_snapshot_id=snapshot.object_id,
            universe_symbols=list(_SYMBOLS),
            run_dir="direct-run",
        )
    )
    from_similarity = json.loads(
        DraftStrategyTool(
            default_session_id="session-similarity",
            research_store=research_store,
            version_db_path=version_db,
        ).execute(
            instruction="用这三只相似股每月选波动最低的三只，风险低一些",
            proposal=_proposal(),
            similarity_run_id=similarity.object_id,
            run_dir="similarity-run",
        )
    )

    assert direct["status"] == from_similarity["status"] == "ready"
    assert direct["worker_started"] is from_similarity["worker_started"] is False
    direct_spec = direct["visualizations"][0]
    similarity_spec = from_similarity["visualizations"][0]
    assert direct_spec["type"] == similarity_spec["type"] == "strategy_confirmation"
    assert load_visualization_specs(run_direct) == [direct_spec]
    assert load_visualization_specs(run_similarity) == [similarity_spec]
    direct_payload = StrategyConfirmationVisualizationPayload.model_validate_json(
        (
            run_direct
            / "artifacts"
            / "visualizations"
            / f"{direct_spec['visualization_id']}.json"
        ).read_text(encoding="utf-8")
    )
    similarity_payload = StrategyConfirmationVisualizationPayload.model_validate_json(
        (
            run_similarity
            / "artifacts"
            / "visualizations"
            / f"{similarity_spec['visualization_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert direct_payload.lifecycle_state == "awaiting_confirmation"
    assert similarity_payload.lifecycle_state == "awaiting_confirmation"
    assert direct_payload.card.strategy.universe_symbols == _SYMBOLS
    assert similarity_payload.card.strategy.universe_symbols == _SYMBOLS[1:]
    assert direct_payload.card.defaults
    assert direct_payload.card.strategy.data_snapshot_ref == snapshot.ref()


def test_text_confirmation_records_receipt_without_worker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    research_store, research, snapshot, _ = _seed_store(tmp_path)
    version_db = tmp_path / "versions.db"
    draft_run = tmp_path / "runs" / "draft"
    confirm_run = tmp_path / "runs" / "confirm"
    monkeypatch.setattr(
        "src.tools.strategy_draft_tool.safe_run_dir",
        lambda value: draft_run if value == "draft-run" else confirm_run,
    )
    drafted = json.loads(
        DraftStrategyTool(
            default_session_id="session-confirm",
            research_store=research_store,
            version_db_path=version_db,
        ).execute(
            instruction="月度低波动三只",
            proposal=_proposal(),
            research_spec_id=research.object_id,
            data_snapshot_id=snapshot.object_id,
            universe_symbols=list(_SYMBOLS),
            run_dir="draft-run",
        )
    )
    confirmed = json.loads(
        ConfirmStrategyTool(
            default_session_id="session-confirm",
            research_store=research_store,
            version_db_path=version_db,
        ).execute(
            expected_head=drafted["head"],
            confirmation_hash=drafted["confirmation_hash"],
            idempotency_key="chat-confirm-1",
            run_dir="confirm-run",
        )
    )

    assert confirmed["status"] == "confirmed"
    assert confirmed["worker_started"] is False
    with StrategyVersionStore(version_db) as store:
        assert store.get_head("session-confirm").state == "confirmed"
        assert len(store.list_events("session-confirm")) == 3
    spec = confirmed["visualizations"][0]
    payload = StrategyConfirmationVisualizationPayload.model_validate_json(
        (
            confirm_run
            / "artifacts"
            / "visualizations"
            / f"{spec['visualization_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert payload.lifecycle_state == "confirmed"
    assert payload.receipt.receipt_id == confirmed["receipt_id"]


def test_clarification_follow_up_reuses_exact_source_without_repeating_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    research_store, research, snapshot, _ = _seed_store(tmp_path)
    version_db = tmp_path / "versions.db"
    run_dir = tmp_path / "runs" / "clarification"
    monkeypatch.setattr("src.tools.strategy_draft_tool.safe_run_dir", lambda _value: run_dir)
    tool = DraftStrategyTool(
        default_session_id="session-clarification",
        research_store=research_store,
        version_db_path=version_db,
    )
    first = json.loads(
        tool.execute(
            instruction="做一个低风险策略",
            proposal={
                "schema_version": "vibe.strategy-draft.v1",
                "template_id": None,
                "ambiguities": [{
                    "code": "template_choice",
                    "question": "按排名还是阈值？",
                    "options": ["top_n_rebalance", "factor_threshold"],
                }],
            },
            research_spec_id=research.object_id,
            data_snapshot_id=snapshot.object_id,
            universe_symbols=list(_SYMBOLS),
            run_dir="clarify-1",
        )
    )
    second = json.loads(
        tool.execute(
            instruction="按低波动排名，选三只",
            proposal=_proposal(),
            expected_head=first["head"],
            run_dir="clarify-2",
        )
    )

    assert first["status"] == "needs_clarification"
    assert second["status"] == "ready"
    assert second["version_number"] == 2
    with StrategyVersionStore(version_db) as store:
        versions = store.list_versions("session-clarification")
        assert versions[0].source_context == versions[1].source_context
        assert versions[1].strategy.universe_symbols == _SYMBOLS


def test_history_resolves_current_head_and_rejects_cross_session_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    research_store, research, snapshot, _ = _seed_store(tmp_path)
    version_db = tmp_path / "versions.db"
    run_dir = tmp_path / "runs" / "history"
    monkeypatch.setattr("src.tools.strategy_draft_tool.safe_run_dir", lambda _value: run_dir)
    monkeypatch.setattr(
        "src.session.service.default_strategy_version_db_path",
        lambda: version_db,
    )
    drafted = json.loads(
        DraftStrategyTool(
            default_session_id="session-history",
            research_store=research_store,
            version_db_path=version_db,
        ).execute(
            instruction="月度低波动三只",
            proposal=_proposal(),
            research_spec_id=research.object_id,
            data_snapshot_id=snapshot.object_id,
            universe_symbols=list(_SYMBOLS),
            run_dir="history-run",
        )
    )
    metadata = {"visualizations": drafted["visualizations"]}

    restored = _persisted_strategy_history(metadata, session_id="session-history")
    crossed = _persisted_strategy_history(metadata, session_id="other-session")
    history = SessionService._convert_messages_to_history(
        [
            Message(
                session_id="session-history",
                role="assistant",
                content="已生成确认卡。",
                metadata=metadata,
            ),
            Message(
                session_id="session-history",
                role="user",
                content="改成两只",
            ),
        ]
    )

    assert drafted["head"]["event_id"] in restored
    assert f"revision={drafted['head']['revision']}" in restored
    assert "proposal_json=" in restored
    assert crossed == ""
    assert history[0] == {"role": "assistant", "content": "已生成确认卡。"}
    assert history[1]["role"] == "system"
    assert "<persisted-strategy-version>" in history[1]["content"]


def test_authenticated_api_confirms_exact_card_idempotently_and_restores_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import httpx

    import api_server
    from src.session.events import EventBus
    from src.session.store import SessionStore

    research_store, research, snapshot, _ = _seed_store(tmp_path)
    version_db = tmp_path / "versions.db"
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "api-card"
    class _Index:
        def index_session(self, *_args) -> None:
            pass

        def index_message(self, *_args) -> None:
            pass

    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _Index())
    monkeypatch.setattr(api_server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        api_server,
        "_session_service",
        SessionService(
            SessionStore(tmp_path / "sessions"),
            EventBus(),
            runs_dir,
        ),
    )
    monkeypatch.setattr("src.tools.strategy_draft_tool.safe_run_dir", lambda _value: run_dir)
    monkeypatch.setattr(
        "src.api.strategy_routes.default_strategy_version_db_path",
        lambda: version_db,
    )
    monkeypatch.setattr(
        "src.api.runs_routes.default_strategy_version_db_path",
        lambda: version_db,
    )
    async def scenario():
        transport = httpx.ASGITransport(
            app=api_server.app,
            client=("127.0.0.1", 50000),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post("/sessions", json={"title": "QE4 API"})
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            other_response = await client.post(
                "/sessions",
                json={"title": "QE4 other"},
            )
            other = other_response.json()["session_id"]
            drafted = json.loads(
                DraftStrategyTool(
                    default_session_id=session_id,
                    research_store=research_store,
                    version_db_path=version_db,
                ).execute(
                    instruction="月度低波动三只",
                    proposal=_proposal(),
                    research_spec_id=research.object_id,
                    data_snapshot_id=snapshot.object_id,
                    universe_symbols=list(_SYMBOLS),
                    run_dir="api-run",
                )
            )
            spec = drafted["visualizations"][0]
            endpoint = (
                f"/sessions/{session_id}/runs/api-card/strategy-confirmations/"
                f"{spec['visualization_id']}/confirm"
            )
            request = {
                "expected_head": drafted["head"],
                "confirmation_hash": drafted["confirmation_hash"],
                "idempotency_key": "api-double-click",
            }

            before = await client.get(
                f"/runs/api-card/visualizations/{spec['visualization_id']}"
            )
            first = await client.post(endpoint, json=request)
            retry = await client.post(endpoint, json=request)
            restored = await client.get(
                f"/runs/api-card/visualizations/{spec['visualization_id']}"
            )
            crossed = await client.post(
                endpoint.replace(session_id, other),
                json=request,
            )
            outside = tmp_path / "outside-card.json"
            outside.write_text(
                (
                    run_dir
                    / "artifacts"
                    / "visualizations"
                    / f"{spec['visualization_id']}.json"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            linked_dir = runs_dir / "linked" / "artifacts" / "visualizations"
            linked_dir.mkdir(parents=True)
            (linked_dir / f"{spec['visualization_id']}.json").symlink_to(outside)
            linked = await client.get(
                f"/runs/linked/visualizations/{spec['visualization_id']}"
            )
            with sqlite3.connect(version_db) as connection:
                connection.execute(
                    "UPDATE strategy_heads SET revision=revision + 1 WHERE stream_id=?",
                    (session_id,),
                )
                connection.commit()
            tampered = await client.get(
                f"/runs/api-card/visualizations/{spec['visualization_id']}"
            )
            with sqlite3.connect(version_db) as connection:
                connection.execute(
                    "UPDATE strategy_heads SET revision=revision - 1 WHERE stream_id=?",
                    (session_id,),
                )
                connection.commit()
            return (
                session_id,
                before,
                first,
                retry,
                restored,
                crossed,
                linked,
                tampered,
            )

    (
        session_id,
        before,
        first,
        retry,
        restored,
        crossed,
        linked,
        tampered,
    ) = asyncio.run(scenario())

    assert before.status_code == 200
    assert before.json()["lifecycle_state"] == "awaiting_confirmation"
    assert first.status_code == retry.status_code == 200
    assert first.json()["receipt"]["receipt_id"] == retry.json()["receipt"]["receipt_id"]
    assert first.json()["lifecycle_state"] == "confirmed"
    assert restored.status_code == 200
    assert restored.json()["lifecycle_state"] == "confirmed"
    assert crossed.status_code == 409
    assert linked.status_code == 404
    assert tampered.status_code == 422
    with StrategyVersionStore(version_db) as store:
        assert len(store.list_events(session_id)) == 3
