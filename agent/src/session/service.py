"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")

from src.session.events import EventBus
from src.session.models import (
    Attempt,
    AttemptStatus,
    Message,
    Session,
)
from src.session.search import get_shared_index
from src.session.store import SessionStore
from src.agent.visible_output import (
    SAFE_FILTERED_RESPONSE,
    strip_internal_context_blocks,
)
from src.research.contracts import canonical_json
from src.research.similarity_presentation import SimilarityVisualizationSpec
from src.strategy_spec.presentation import (
    StrategyConfirmationVisualizationSpec,
    default_strategy_version_db_path,
)
from src.strategy_spec.version_store import StrategyVersionStore


_VISUALIZATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_VISUALIZATION_STRING_FIELDS = (
    "title",
    "symbol",
    "market",
    "timeframe",
    "source",
    "adjustment",
    "timezone",
    "requested_start",
    "requested_end",
    "effective_fetch_start",
    "effective_fetch_end",
    "retention_policy",
    "actual_start",
    "actual_end",
    "fetched_at",
    "fallback_text",
)


def load_visualization_specs(run_dir: Path) -> list[Dict[str, Any]]:
    """Load and sanitize chat visualization metadata from a run artifact."""
    manifest_path = run_dir / "artifacts" / "visualizations.json"
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size > 256_000:
            return []
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    specs: list[Dict[str, Any]] = []
    for item in raw[-5:]:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "similarity_ranking":
            try:
                specs.append(
                    SimilarityVisualizationSpec.model_validate(item).model_dump(mode="json")
                )
            except ValueError:
                pass
            continue
        if item.get("type") == "strategy_confirmation":
            try:
                strategy_spec = (
                    StrategyConfirmationVisualizationSpec.model_validate(item)
                )
            except ValueError:
                pass
            else:
                # One agent attempt can legitimately refine a clarification
                # draft into a ready confirmation card.  The manifest keeps
                # both immutable versions for audit, but chat should present
                # only the newest head for that strategy stream.
                specs = [
                    spec
                    for spec in specs
                    if not (
                        spec.get("type") == "strategy_confirmation"
                        and spec.get("stream_id") == strategy_spec.stream_id
                    )
                ]
                specs.append(strategy_spec.model_dump(mode="json"))
            continue
        visualization_id = item.get("visualization_id")
        data_ref = item.get("data_ref")
        if (
            item.get("schema_version") != 1
            or item.get("type") != "candlestick_volume"
            or not isinstance(visualization_id, str)
            or not _VISUALIZATION_ID_RE.fullmatch(visualization_id)
            or not isinstance(data_ref, str)
            or data_ref != visualization_id
        ):
            continue
        spec: Dict[str, Any] = {
            "schema_version": 1,
            "type": "candlestick_volume",
            "visualization_id": visualization_id,
            "data_ref": data_ref,
        }
        for key in _VISUALIZATION_STRING_FIELDS:
            value = item.get(key)
            if isinstance(value, str):
                spec[key] = value[:500]
        for field in ("bar_count", "dropped_bar_count"):
            count = item.get(field)
            if (
                isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10_000
            ):
                spec[field] = count
        if isinstance(item.get("truncated"), bool):
            spec["truncated"] = item["truncated"]
        specs.append(spec)
    return specs


def _persisted_similarity_history(metadata: Any) -> str:
    """Expose only validated immutable similarity references to the next turn."""
    if not isinstance(metadata, dict):
        return ""
    raw = metadata.get("visualizations")
    if not isinstance(raw, list):
        return ""
    references: list[str] = []
    seen: set[str] = set()
    for item in raw[-5:]:
        if not isinstance(item, dict) or item.get("type") != "similarity_ranking":
            continue
        try:
            spec = SimilarityVisualizationSpec.model_validate(item)
        except ValueError:
            continue
        if spec.similarity_run_id in seen:
            continue
        seen.add(spec.similarity_run_id)
        references.append(
            f"- similarity_run_id={spec.similarity_run_id}; "
            f"visible_candidate_count={spec.candidate_count}; "
            f"as_of={spec.as_of.isoformat()}"
        )
    if not references:
        return ""
    return (
        "<persisted-similarity-results>\n"
        + "\n".join(references)
        + "\nReuse an exact ID with show_similarity_result before answering "
        "candidate-level follow-ups.\n</persisted-similarity-results>"
    )


def _persisted_strategy_history(metadata: Any, *, session_id: str) -> str:
    """Resolve trusted card metadata to the canonical current session head."""

    if not isinstance(metadata, dict):
        return ""
    raw = metadata.get("visualizations")
    if not isinstance(raw, list):
        return ""
    candidates: list[StrategyConfirmationVisualizationSpec] = []
    for item in raw[-5:]:
        if not isinstance(item, dict) or item.get("type") != "strategy_confirmation":
            continue
        try:
            spec = StrategyConfirmationVisualizationSpec.model_validate(item)
        except ValueError:
            continue
        if spec.stream_id == session_id:
            candidates.append(spec)
    if not candidates:
        return ""
    try:
        with StrategyVersionStore(default_strategy_version_db_path()) as store:
            head = store.get_head(session_id)
            if head is None:
                return ""
            version = store.get_version(head.version_id)
            if version is None:
                return ""
            events = store.list_events(session_id)
            current_event = events[-1] if events else None
    except Exception:
        return ""
    confirmation_hash = (
        current_event.confirmation_hash if current_event is not None else None
    )
    proposal_json = canonical_json(version.proposal)[:6_000]
    return (
        "<persisted-strategy-version>\n"
        f"stream_id={session_id}\n"
        f"version_id={version.version_id}\n"
        f"version_number={version.version_number}\n"
        f"state={head.state}\n"
        f"event_id={head.event_id}\n"
        f"revision={head.revision}\n"
        f"confirmation_hash={confirmation_hash or ''}\n"
        f"proposal_json={proposal_json}\n"
        "For a modification call draft_strategy with this exact expected_head "
        "and a complete replacement proposal. For an explicit confirmation call "
        "confirm_strategy with this exact expected_head/hash and a fresh "
        "idempotency key. Never reuse this block in another session.\n"
        "</persisted-strategy-version>"
    )


class SessionService:
    """Session lifecycle service.

    Attributes:
        store: Session persistence store.
        event_bus: SSE event bus.
        runs_dir: Root runs directory.
    """

    def __init__(
        self,
        store: SessionStore,
        event_bus: EventBus,
        runs_dir: Path,
    ) -> None:
        """Initialize the session service.

        Args:
            store: Session persistence store.
            event_bus: SSE event bus.
            runs_dir: Root runs directory.
        """
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        self._active_loops: Dict[str, "AgentLoop"] = {}
        self._search_index = get_shared_index()

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {})
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID."""
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        self.event_bus.clear(session_id)
        return self.store.delete_session(session_id)

    async def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        """Send a message to a session and trigger execution.

        Args:
            session_id: Session ID.
            content: Message content.
            role: Message role.
            include_shell_tools: Whether this attempt may use shell tools.

        Returns:
            Dictionary containing message_id and attempt_id.
        """
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        if role == "assistant":
            content = strip_internal_context_blocks(content) or SAFE_FILTERED_RESPONSE
        message = Message(session_id=session_id, role=role, content=content)
        self.store.append_message(message)
        self._search_index.index_message(session_id, role, content)
        self.event_bus.emit(session_id, "message.received", {"message_id": message.message_id, "role": role, "content": content})

        if role != "user":
            return {"message_id": message.message_id}

        attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
        self.store.create_attempt(attempt)
        session.config["include_shell_tools"] = include_shell_tools
        session.last_attempt_id = attempt.attempt_id
        session.updated_at = datetime.now().isoformat()
        self.store.update_session(session)
        self.event_bus.emit(session_id, "attempt.created", {"attempt_id": attempt.attempt_id, "prompt": content})

        asyncio.create_task(self._run_attempt(session, attempt, include_shell_tools=include_shell_tools))
        return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        """Return the message history."""
        messages = self.store.get_messages(session_id, limit)
        return [
            replace(
                message,
                content=(
                    strip_internal_context_blocks(message.content)
                    or SAFE_FILTERED_RESPONSE
                ),
            )
            if message.role == "assistant"
            else message
            for message in messages
        ]

    def cancel_current(self, session_id: str) -> bool:
        """Cancel the currently running AgentLoop for a session.

        Args:
            session_id: Session ID.

        Returns:
            Whether cancellation succeeded. True means an active loop existed and received a cancel signal.
        """
        loop = self._active_loops.get(session_id)
        if loop is None:
            return False
        loop.cancel()
        return True

    async def _run_attempt(self, session: Session, attempt: Attempt, *, include_shell_tools: bool = False) -> None:
        """Execute an Attempt in the background."""
        attempt.mark_running()
        self.store.update_attempt(attempt)
        self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})

        try:
            messages = self.store.get_messages(session.session_id)
            result = await self._run_with_agent(
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=dict(session.config),
            )
            if result.get("status") == "success":
                visible_content = strip_internal_context_blocks(
                    str(result.get("content") or "")
                )
                if result.get("content") and not visible_content:
                    visible_content = SAFE_FILTERED_RESPONSE
                result["content"] = visible_content
                attempt.mark_completed(summary=visible_content)
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")

            self.store.update_attempt(attempt)
            reply_metadata = {}
            visualizations: list[Dict[str, Any]] = []
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
                visualizations = load_visualization_specs(Path(attempt.run_dir))
                if visualizations:
                    reply_metadata["visualizations"] = visualizations
            reply_metadata["status"] = attempt.status.value
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics

            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
            )
            self.store.append_message(reply)
            self._search_index.index_message(session.session_id, "assistant", reply.content)
            self.event_bus.emit(
                session.session_id,
                "attempt.completed" if attempt.status == AttemptStatus.COMPLETED else "attempt.failed",
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "summary": attempt.summary, "error": attempt.error, "run_dir": attempt.run_dir,
                 "visualizations": visualizations},
            )

        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            self.event_bus.emit(session.session_id, "attempt.failed", {"attempt_id": attempt.attempt_id, "error": str(exc)})

    async def _run_with_agent(
        self,
        attempt: Attempt,
        messages: list = None,
        *,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an attempt with the V5 AgentLoop.

        Args:
            attempt: Current execution attempt.
            messages: Session message history.
            include_shell_tools: Whether the registry may include shell tools.
            session_config: Optional session-level config overrides. MCP server
                definitions under the ``mcpServers`` key are merged on top of
                the user config file via ``load_runtime_agent_config`` so each
                session can extend or override the global MCP server list.

        Returns:
            Result dictionary containing status, run_dir, run_id, metrics, and related fields.
        """
        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides

        llm = ChatLLM()
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        registry = await loop.run_in_executor(
            _AGENT_EXECUTOR,
            lambda: build_registry(
                persistent_memory=pm,
                include_shell_tools=include_shell_tools,
                agent_config=agent_config,
                session_id=session_id,
                event_callback=event_callback,
                warn_callback=_mcp_collision_warn,
            ),
        )

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=50,
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        # Build the message history context.
        history = self._convert_messages_to_history(messages) if messages else None

        try:
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                ),
            )
        finally:
            self._active_loops.pop(session_id, None)

        # Load metrics from the run output when available.
        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics

        return result

    @staticmethod
    def _convert_messages_to_history(messages: list) -> list[Dict[str, Any]]:
        """Convert Session messages into OpenAI-format history.

        Keeps the readable ``[prev_run: {run_id}]`` marker instead of removing it
        completely, and trims by character budget instead of a hard six-message cap
        so the LLM can still see previous artifact paths and strategy content during
        iterative updates.

        Args:
            messages: Session message list without the current turn.

        Returns:
            OpenAI-format messages trimmed from the newest items within the token budget.
        """
        import re
        from pathlib import Path

        def _shorten_run_dir(match: re.Match) -> str:
            path_str = match.group(0).replace("Run directory:", "").strip()
            run_id = Path(path_str).name if path_str else ""
            return f"[prev_run: {run_id}]" if run_id else ""

        history = []
        for msg in messages[:-1]:
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            if not content.strip() or role not in ("user", "assistant"):
                continue
            if role == "assistant":
                content = strip_internal_context_blocks(content)
                if not content:
                    content = SAFE_FILTERED_RESPONSE
            content = re.sub(r"Run directory:\s*\S+", _shorten_run_dir, content).strip()
            if content:
                metadata = msg.metadata if hasattr(msg, "metadata") else msg.get("metadata", {})
                history.append({"role": role, "content": content})
                trusted_context: list[str] = []
                similarity_history = (
                    _persisted_similarity_history(metadata) if role == "assistant" else ""
                )
                if similarity_history:
                    trusted_context.append(similarity_history)
                strategy_history = (
                    _persisted_strategy_history(
                        metadata,
                        session_id=(
                            msg.session_id
                            if hasattr(msg, "session_id")
                            else str(msg.get("session_id") or "")
                        ),
                    )
                    if role == "assistant"
                    else ""
                )
                if strategy_history:
                    trusted_context.append(strategy_history)
                if trusted_context:
                    history.append(
                        {
                            "role": "system",
                            "content": (
                                "Trusted same-session recovery context. Use it only "
                                "for exact tool arguments. Never quote or expose it "
                                "in assistant output.\n\n"
                                + "\n\n".join(trusted_context)
                            ),
                        }
                    )

        # Trim from the newest messages within a character budget of roughly 3000 tokens.
        MAX_HISTORY_CHARS = 12000
        total_chars = 0
        trimmed: list = []
        for msg in reversed(history):
            msg_len = len(msg.get("content", ""))
            if total_chars + msg_len > MAX_HISTORY_CHARS:
                break
            trimmed.append(msg)
            total_chars += msg_len
        return list(reversed(trimmed))

    @staticmethod
    def _load_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Load metrics.csv from a run directory."""
        import csv
        metrics_path = run_dir / "artifacts" / "metrics.csv"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return {k: float(v) for k, v in rows[0].items() if v}
        except Exception:
            pass
        return None

    @staticmethod
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message."""
        if attempt.status == AttemptStatus.COMPLETED:
            return (
                strip_internal_context_blocks(attempt.summary)
                or "Strategy execution completed."
            )
        return f"Execution failed: {attempt.error or 'unknown error'}"
