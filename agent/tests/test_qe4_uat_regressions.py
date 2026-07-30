"""Production browser regressions discovered during the QE4 household UAT."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from src.agent.loop import AgentLoop
from src.agent.tools import ToolRegistry
from src.agent.visible_output import (
    SAFE_FILTERED_RESPONSE,
    VisibleAssistantStreamFilter,
    strip_internal_context_blocks,
)
from src.memory.persistent import PersistentMemory
from src.research.store import ResearchStore
from src.session.models import Message
from src.session.service import SessionService
from src.tools.strategy_draft_tool import DraftStrategyTool


_SIMILARITY_ID = "similarity_run:" + "a" * 64
_RESEARCH_ID = "research_spec:" + "b" * 64
_SNAPSHOT_ID = "data_snapshot_ref:" + "c" * 64


def _proposal() -> dict[str, Any]:
    return {
        "schema_version": "vibe.strategy-draft.v1",
        "template_id": "top_n_rebalance",
        "title": "低波动月度策略",
        "ranking_field": "volatility_20d",
        "ranking_direction": "ascending",
        "top_n": 3,
        "rebalance": "monthly",
        "risk_level": "low",
    }


def test_qe4_uat_01_source_schema_is_mutually_exclusive_and_error_is_typed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    schema = DraftStrategyTool.parameters
    assert len(schema["oneOf"]) == 3
    assert "source forms are mutually exclusive" in DraftStrategyTool.description

    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        "src.tools.strategy_draft_tool.safe_run_dir",
        lambda _value: run_dir,
    )
    result = json.loads(
        DraftStrategyTool(
            default_session_id="qe4-uat-source-conflict",
            research_store=ResearchStore(tmp_path / "research"),
            version_db_path=tmp_path / "versions.db",
        ).execute(
            instruction="基于相似股结果选择20日波动率最低的3只",
            proposal=_proposal(),
            similarity_run_id=_SIMILARITY_ID,
            research_spec_id=_RESEARCH_ID,
            data_snapshot_id=_SNAPSHOT_ID,
            universe_symbols=["600519.SH"],
            run_dir="ignored-by-test",
        )
    )

    assert result == {
        "status": "error",
        "error_code": "strategy_source_conflict",
        "error": "策略来源冲突：SimilarityRun 不能与直接 research/snapshot/universe 输入同时使用。",
        "user_message": "策略来源冲突：SimilarityRun 不能与直接 research/snapshot/universe 输入同时使用。",
        "recovery": (
            "若要基于相似股结果继续，只保留 exact similarity_run_id 后重试；"
            "不要改写或猜测策略字段。"
        ),
        "worker_started": False,
    }
    assert not run_dir.exists()


def test_qe4_uat_02_complete_and_unterminated_internal_blocks_are_removed() -> None:
    leaked = (
        "确认卡已生成。\n"
        "<persisted-strategy-version>\n"
        "confirmation_hash=secret\nproposal_json={\"private\":true}\n"
        "</persisted-strategy-version>\n"
        "请查看卡片。"
    )
    assert strip_internal_context_blocks(leaked) == "确认卡已生成。\n\n请查看卡片。"
    assert (
        strip_internal_context_blocks(
            "安全摘要\n<persisted-similarity-results>\nsecret"
        )
        == "安全摘要"
    )
    assert strip_internal_context_blocks("安全摘要\n<persisted-strategy-ver") == (
        "安全摘要"
    )


def test_qe4_uat_02_stream_filter_drops_partial_internal_opening_on_finish() -> None:
    stream_filter = VisibleAssistantStreamFilter()
    visible = stream_filter.feed("安全摘要\n<persisted-strategy-version")
    visible += stream_filter.finish()

    assert visible == "安全摘要\n"


def test_qe4_uat_02_stream_filter_handles_markers_split_across_chunks() -> None:
    stream_filter = VisibleAssistantStreamFilter()
    chunks = (
        "确认卡已生成。\n<persisted-strat",
        "egy-version>\nconfirmation_hash=secret\n",
        "</persisted-strategy-ver",
        "sion>\n请查看卡片。",
    )
    visible = "".join(stream_filter.feed(chunk) for chunk in chunks)
    visible += stream_filter.finish()

    assert visible == "确认卡已生成。\n\n请查看卡片。"
    assert "persisted-" not in visible
    assert "secret" not in visible


class _LeakyResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls: list[Any] = []
        self.reasoning_content: str | None = None
        self.has_tool_calls = False
        self.usage_metadata: dict[str, int] = {}
        self.content_filter_triggered = False


class _LeakyStreamingLLM:
    def __init__(self, chunks: tuple[str, ...]) -> None:
        self.chunks = chunks
        self.content = "".join(chunks)

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> _LeakyResponse:
        del messages, tools, on_reasoning_chunk, should_cancel
        for chunk in self.chunks:
            if on_text_chunk is not None:
                on_text_chunk(chunk)
        return _LeakyResponse(self.content)

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> _LeakyResponse:
        del messages
        return _LeakyResponse("")


class _TypedDraftErrorTool:
    name = "draft_strategy"
    description = "Return one typed source conflict for deterministic error projection."
    parameters = {"type": "object", "properties": {}}
    repeatable = True
    is_readonly = False
    requires_current_run_dir = False

    @classmethod
    def check_available(cls) -> bool:
        return True

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs: Any) -> str:
        del kwargs
        return json.dumps(
            {
                "status": "error",
                "error_code": "strategy_source_conflict",
                "user_message": "策略来源冲突：只能使用一种来源。",
                "recovery": "只保留 exact similarity_run_id 后重试。",
                "worker_started": False,
            },
            ensure_ascii=False,
        )


class _ScriptedTypedErrorLLM:
    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> _LeakyResponse:
        del messages, tools, on_text_chunk, on_reasoning_chunk, should_cancel
        self.calls += 1
        if self.calls == 1:
            response = _LeakyResponse("")
            response.tool_calls = [
                SimpleNamespace(
                    id="draft-error-1",
                    name="draft_strategy",
                    arguments={},
                )
            ]
            response.has_tool_calls = True
            return response
        return _LeakyResponse("volatility_20d 不在白名单。")

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> _LeakyResponse:
        del messages
        return _LeakyResponse("")


def test_qe4_uat_01_typed_source_error_overrides_model_misattribution(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    registry.register(_TypedDraftErrorTool())
    memory = PersistentMemory(memory_dir=tmp_path / "memory")
    agent = AgentLoop(
        registry=registry,
        llm=_ScriptedTypedErrorLLM(),
        max_iterations=2,
        persistent_memory=memory,
    )
    agent.memory.run_dir = str(tmp_path / "run")

    result = agent.run("生成低波动确认卡")

    assert result["status"] == "success"
    assert result["content"] == (
        "策略来源冲突：只能使用一种来源。\n\n"
        "下一步：只保留 exact similarity_run_id 后重试。\n\n"
        "未启动回测 worker。"
    )
    assert "白名单" not in result["content"]


def test_qe4_uat_02_agent_loop_filters_live_sse_and_final_content(
    tmp_path: Path,
) -> None:
    chunks = (
        "已生成确认卡。\n<persisted-strategy-",
        "version>\nconfirmation_hash=secret\n",
        "</persisted-strategy-version>\n未启动 worker。",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    memory = PersistentMemory(memory_dir=tmp_path / "memory")
    agent = AgentLoop(
        registry=ToolRegistry(),
        llm=_LeakyStreamingLLM(chunks),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        max_iterations=1,
        persistent_memory=memory,
    )
    agent.memory.run_dir = str(tmp_path / "run")

    result = agent.run("生成确认卡")
    visible_deltas = "".join(
        payload["delta"] for kind, payload in events if kind == "text_delta"
    )

    assert result["status"] == "success"
    assert result["content"] == "已生成确认卡。\n\n未启动 worker。"
    assert visible_deltas == result["content"]
    assert "persisted-" not in visible_deltas
    assert "secret" not in visible_deltas


def test_qe4_uat_02_session_history_and_reads_hide_legacy_leaks() -> None:
    legacy = Message(
        session_id="session-1",
        role="assistant",
        content=(
            "已生成。\n<persisted-strategy-version>\n"
            "confirmation_hash=secret\n</persisted-strategy-version>\n完成。"
        ),
    )
    user = Message(session_id="session-1", role="user", content="继续")
    history = SessionService._convert_messages_to_history([legacy, user])

    class _LegacyStore:
        @staticmethod
        def get_messages(_session_id: str, _limit: int) -> list[Message]:
            return [legacy]

    service = object.__new__(SessionService)
    service.store = _LegacyStore()
    visible_messages = service.get_messages("session-1")

    assert history == [{"role": "assistant", "content": "已生成。\n\n完成。"}]
    assert visible_messages[0].content == "已生成。\n\n完成。"
    assert "secret" in legacy.content


def test_qe4_uat_02_only_internal_context_gets_safe_fallback() -> None:
    content = (
        "<persisted-similarity-results>\n"
        "similarity_run_id=secret\n"
        "</persisted-similarity-results>"
    )
    assert strip_internal_context_blocks(content) == ""

    class _LegacyStore:
        @staticmethod
        def get_messages(_session_id: str, _limit: int) -> list[Message]:
            return [Message(session_id="session-1", role="assistant", content=content)]

    service = object.__new__(SessionService)
    service.store = _LegacyStore()
    assert service.get_messages("session-1")[0].content == SAFE_FILTERED_RESPONSE
