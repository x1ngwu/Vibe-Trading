"""QE4 product tools for drafting and confirming immutable StrategySpec versions."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.research.contracts import (
    DEFAULT_OWNER_SCOPE,
    DataSnapshotRef,
    ResearchSpec,
    SimilarityRun,
    canonical_json,
)
from src.research.store import ResearchStore
from src.strategy_spec.drafting import (
    StrategyDraftProposal,
    StrategyDraftRequest,
    draft_strategy_from_language,
)
from src.strategy_spec.presentation import (
    StrategyDataBasis,
    build_strategy_visualization,
    default_strategy_version_db_path,
    persist_strategy_visualization,
)
from src.strategy_spec.templates import StrategyTemplateSource
from src.strategy_spec.version_store import StrategyVersionStore
from src.strategy_spec.versioning import StrategyHeadToken, StrategyVersionSource
from src.tools.path_utils import safe_run_dir

_OBJECT_ID = re.compile(
    r"^(?:research_spec|data_snapshot_ref|similarity_run):[0-9a-f]{64}$"
)
_CONFIRMATION_TTL_MINUTES = 15


class StrategySourceError(ValueError):
    """Stable, user-explainable failure at the strategy source boundary."""

    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery

    def as_tool_result(self) -> str:
        return canonical_json(
            {
                "status": "error",
                "error_code": self.code,
                "error": str(self),
                "user_message": str(self),
                "recovery": self.recovery,
                "worker_started": False,
            }
        )


class _ProposalModel:
    """Return the already schema-bound Agent function arguments as model JSON."""

    def __init__(self, proposal: StrategyDraftProposal) -> None:
        self._proposal = proposal

    def generate(self, _messages) -> str:
        return canonical_json(self._proposal)


def _require_object(store: ResearchStore, object_id: str, object_type: str):
    if not _OBJECT_ID.fullmatch(object_id) or not object_id.startswith(f"{object_type}:"):
        raise ValueError(f"{object_type}_id must be a canonical content-addressed object ID")
    obj = store.get(object_id)
    if obj is None or obj.object_type != object_type:
        raise ValueError(f"{object_type} was not found in the household research store")
    return obj


def _source_from_kwargs(store: ResearchStore, kwargs: dict[str, Any]) -> StrategyTemplateSource:
    similarity_run_id = str(kwargs.get("similarity_run_id") or "").strip()
    if similarity_run_id:
        if any(
            kwargs.get(key)
            for key in ("research_spec_id", "data_snapshot_id", "universe_symbols")
        ):
            raise StrategySourceError(
                "strategy_source_conflict",
                "策略来源冲突：SimilarityRun 不能与直接 research/snapshot/universe 输入同时使用。",
                "若要基于相似股结果继续，只保留 exact similarity_run_id 后重试；"
                "不要改写或猜测策略字段。",
            )
        similarity = _require_object(store, similarity_run_id, "similarity_run")
        payload = SimilarityRun.model_validate(similarity.payload)
        research = _require_object(
            store, payload.research_spec_ref.object_id, "research_spec"
        )
        snapshot = _require_object(
            store, payload.data_snapshot_ref.object_id, "data_snapshot_ref"
        )
        return StrategyTemplateSource(
            research=research,
            snapshot=snapshot,
            similarity_run=similarity,
        )

    research_id = str(kwargs.get("research_spec_id") or "").strip()
    snapshot_id = str(kwargs.get("data_snapshot_id") or "").strip()
    symbols = tuple(kwargs.get("universe_symbols") or ())
    if not research_id or not snapshot_id or not symbols:
        raise StrategySourceError(
            "direct_strategy_source_incomplete",
            "直接策略来源不完整：必须同时提供 research_spec_id、data_snapshot_id 和 universe_symbols。",
            "补齐同一研究对象的三个直接来源字段，或改为只提供 exact similarity_run_id。",
        )
    research = _require_object(store, research_id, "research_spec")
    snapshot = _require_object(store, snapshot_id, "data_snapshot_ref")
    ResearchSpec.model_validate(research.payload)
    DataSnapshotRef.model_validate(snapshot.payload)
    return StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=symbols,
    )


def _source_from_version(store: ResearchStore, version) -> StrategyTemplateSource | None:
    context = version.source_context
    if context is not None:
        research = _require_object(
            store, context.research_spec_ref.object_id, "research_spec"
        )
        snapshot = _require_object(
            store, context.data_snapshot_ref.object_id, "data_snapshot_ref"
        )
        if context.similarity_run_ref is not None:
            similarity = _require_object(
                store, context.similarity_run_ref.object_id, "similarity_run"
            )
            return StrategyTemplateSource(
                research=research,
                snapshot=snapshot,
                similarity_run=similarity,
            )
        return StrategyTemplateSource(
            research=research,
            snapshot=snapshot,
            universe_symbols=context.universe_symbols,
        )
    strategy = version.strategy
    if strategy is None or strategy.research_spec_ref is None:
        return None
    research = _require_object(
        store, strategy.research_spec_ref.object_id, "research_spec"
    )
    snapshot = _require_object(
        store, strategy.data_snapshot_ref.object_id, "data_snapshot_ref"
    )
    if strategy.similarity_run_ref is not None:
        similarity = _require_object(
            store, strategy.similarity_run_ref.object_id, "similarity_run"
        )
        return StrategyTemplateSource(
            research=research,
            snapshot=snapshot,
            similarity_run=similarity,
        )
    return StrategyTemplateSource(
        research=research,
        snapshot=snapshot,
        universe_symbols=strategy.universe_symbols,
    )


def _data_basis(source: StrategyTemplateSource) -> StrategyDataBasis:
    snapshot = DataSnapshotRef.model_validate(source.snapshot.payload)
    return StrategyDataBasis.from_snapshot(source.snapshot.ref(), snapshot)


def _version_source(source: StrategyTemplateSource) -> StrategyVersionSource:
    return StrategyVersionSource(
        research_spec_ref=source.research.ref(),
        data_snapshot_ref=source.snapshot.ref(),
        similarity_run_ref=(
            source.similarity_run.ref()
            if source.similarity_run is not None
            else None
        ),
        universe_symbols=source.resolved_universe,
    )


class DraftStrategyTool(BaseTool):
    """Create an immutable strategy draft and, when ready, an exact confirmation card."""

    name = "draft_strategy"
    description = (
        "Compile a natural-language household A-share strategy into the strict QE4 "
        "StrategySpec path. The Agent supplies one schema-bound proposal plus either "
        "an exact SimilarityRun ID or exact research/snapshot IDs and symbols; these "
        "source forms are mutually exclusive. On a typed source error, report its "
        "exact user_message and recovery instead of inferring a different cause. This "
        "tool creates a draft/child version and confirmation card only; it never "
        "starts a backtest worker."
    )
    parameters = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
            "proposal": StrategyDraftProposal.model_json_schema(),
            "similarity_run_id": {
                "type": "string",
                "pattern": r"^similarity_run:[0-9a-f]{64}$",
            },
            "research_spec_id": {
                "type": "string",
                "pattern": r"^research_spec:[0-9a-f]{64}$",
            },
            "data_snapshot_id": {
                "type": "string",
                "pattern": r"^data_snapshot_ref:[0-9a-f]{64}$",
            },
            "universe_symbols": {
                "type": "array",
                "items": {"type": "string", "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,31}$"},
                "minItems": 1,
                "maxItems": 500,
                "uniqueItems": True,
            },
            "expected_head": StrategyHeadToken.model_json_schema(),
            "untrusted_context": {
                "type": "object",
                "additionalProperties": {"type": "string", "maxLength": 2000},
                "maxProperties": 64,
            },
        },
        "required": ["instruction", "proposal"],
        "oneOf": [
            {
                "required": ["similarity_run_id"],
                "not": {
                    "anyOf": [
                        {"required": ["research_spec_id"]},
                        {"required": ["data_snapshot_id"]},
                        {"required": ["universe_symbols"]},
                    ]
                },
            },
            {
                "required": [
                    "research_spec_id",
                    "data_snapshot_id",
                    "universe_symbols",
                ],
                "not": {"required": ["similarity_run_id"]},
            },
            {
                "required": ["expected_head"],
                "not": {
                    "anyOf": [
                        {"required": ["similarity_run_id"]},
                        {"required": ["research_spec_id"]},
                        {"required": ["data_snapshot_id"]},
                        {"required": ["universe_symbols"]},
                    ]
                },
            },
        ],
    }
    repeatable = True
    is_readonly = False
    requires_current_run_dir = True

    def __init__(
        self,
        *,
        default_session_id: str | None = None,
        event_callback=None,
        research_store: ResearchStore | None = None,
        version_db_path: Path | str | None = None,
    ) -> None:
        self._session_id = default_session_id
        self._event_callback = event_callback
        self._research_store = research_store
        self._version_db_path = Path(version_db_path) if version_db_path else None

    def execute(self, **kwargs: Any) -> str:
        if not self._session_id:
            raise ValueError("draft_strategy requires an injected session identity")
        run_dir_raw = str(kwargs.get("run_dir") or "").strip()
        if not run_dir_raw:
            raise ValueError("run_dir is required")
        run_dir = safe_run_dir(run_dir_raw)
        instruction = str(kwargs.get("instruction") or "").strip()
        proposal = StrategyDraftProposal.model_validate(kwargs.get("proposal"))
        request = StrategyDraftRequest(
            user_text=instruction,
            untrusted_context=kwargs.get("untrusted_context") or {},
        )
        research_store = self._research_store or ResearchStore.default()
        db_path = self._version_db_path or default_strategy_version_db_path()

        expected_raw = kwargs.get("expected_head")
        expected_head = (
            StrategyHeadToken.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )
        try:
            with StrategyVersionStore(db_path) as version_store:
                current = version_store.get_head(self._session_id)
                source = None
                explicit_source = bool(
                    kwargs.get("similarity_run_id")
                    or kwargs.get("research_spec_id")
                    or kwargs.get("data_snapshot_id")
                    or kwargs.get("universe_symbols")
                )
                if current is not None:
                    if expected_head is None:
                        raise ValueError(
                            "strategy stream already exists; pass the exact current expected_head"
                        )
                    if expected_head.stream_id != self._session_id:
                        raise ValueError("expected_head belongs to another session")
                    current_version = version_store.get_version(current.version_id)
                    if explicit_source:
                        source = _source_from_kwargs(research_store, kwargs)
                        if (
                            current_version is not None
                            and current_version.source_context is not None
                            and _version_source(source)
                            != current_version.source_context
                        ):
                            raise StrategySourceError(
                                "strategy_source_change_forbidden",
                                "策略修改不能切换数据来源；请保持当前版本绑定的研究对象和快照。",
                                "仅传 exact expected_head 和完整 replacement proposal 后重试。",
                            )
                    elif current_version is not None:
                        source = _source_from_version(research_store, current_version)
                elif expected_head is not None:
                    raise ValueError("cannot modify a strategy stream that does not exist")
                if source is None:
                    source = _source_from_kwargs(research_store, kwargs)

                result = draft_strategy_from_language(
                    _ProposalModel(proposal),
                    request,
                    source=source,
                )
                if result.status == "rejected":
                    return canonical_json(
                        {
                            "status": "rejected",
                            "issues": [
                                issue.model_dump(mode="json") for issue in result.issues
                            ],
                            "security_warnings": [
                                item.model_dump(mode="json")
                                for item in result.security_warnings
                            ],
                            "worker_started": False,
                        }
                    )
                if result.build is not None:
                    research_store.put(result.build.strategy_object)
                if current is None:
                    version, head = version_store.create_initial_version(
                        stream_id=self._session_id,
                        owner_scope=DEFAULT_OWNER_SCOPE,
                        result=result,
                        source_context=_version_source(source),
                    )
                else:
                    assert expected_head is not None
                    version, head = version_store.modify_version(
                        stream_id=self._session_id,
                        expected_head=expected_head,
                        result=result,
                        source_context=_version_source(source),
                    )

                card = None
                if result.status == "ready":
                    issued_at = datetime.now(timezone.utc)
                    card, head = version_store.prepare_confirmation(
                        stream_id=self._session_id,
                        expected_head=head,
                        issued_at=issued_at,
                        expires_at=issued_at + timedelta(
                            minutes=_CONFIRMATION_TTL_MINUTES
                        ),
                    )
                spec, payload = build_strategy_visualization(
                    version=version,
                    head=head,
                    card=card,
                    data_basis=_data_basis(source),
                )
                persist_strategy_visualization(run_dir, spec, payload)
        except StrategySourceError as exc:
            return exc.as_tool_result()

        if self._event_callback is not None:
            self._event_callback(
                "strategy.version.created",
                {
                    "version_id": version.version_id,
                    "version_number": version.version_number,
                    "state": head.state,
                    "visualization_id": spec.visualization_id,
                },
            )
        return canonical_json(
            {
                "status": result.status,
                "version_id": version.version_id,
                "version_number": version.version_number,
                "head": head.model_dump(mode="json"),
                "confirmation_hash": (
                    card.confirmation_hash if card is not None else None
                ),
                "clarifications": [
                    item.model_dump(mode="json") for item in version.clarifications
                ],
                "visualizations": [spec.model_dump(mode="json")],
                "worker_started": False,
            }
        )


class ConfirmStrategyTool(BaseTool):
    """Confirm one exact current card from a persisted session-history token."""

    name = "confirm_strategy"
    description = (
        "Confirm the exact current QE4 strategy card. Use only the stream/version/"
        "event/revision/hash values restored from trusted strategy history. This "
        "records a receipt and does not start a backtest worker."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expected_head": StrategyHeadToken.model_json_schema(),
            "confirmation_hash": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
            "idempotency_key": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            },
        },
        "required": ["expected_head", "confirmation_hash", "idempotency_key"],
    }
    repeatable = True
    is_readonly = False
    requires_current_run_dir = True

    def __init__(
        self,
        *,
        default_session_id: str | None = None,
        event_callback=None,
        research_store: ResearchStore | None = None,
        version_db_path: Path | str | None = None,
    ) -> None:
        self._session_id = default_session_id
        self._event_callback = event_callback
        self._research_store = research_store
        self._version_db_path = Path(version_db_path) if version_db_path else None

    def execute(self, **kwargs: Any) -> str:
        if not self._session_id:
            raise ValueError("confirm_strategy requires an injected session identity")
        run_dir = safe_run_dir(str(kwargs.get("run_dir") or ""))
        expected = StrategyHeadToken.model_validate(kwargs.get("expected_head"))
        if expected.stream_id != self._session_id:
            raise ValueError("expected_head belongs to another session")
        confirmation_hash = str(kwargs.get("confirmation_hash") or "")
        idempotency_key = str(kwargs.get("idempotency_key") or "")
        db_path = self._version_db_path or default_strategy_version_db_path()
        with StrategyVersionStore(db_path) as store:
            receipt = store.confirm(
                stream_id=self._session_id,
                expected_head=expected,
                confirmation_hash=confirmation_hash,
                idempotency_key=idempotency_key,
                actor_id="household-user",
            )
            version = store.get_version(receipt.version_id)
            head = store.get_head(self._session_id)
            card = store.get_confirmation_card(receipt.confirmation_hash)
            if version is None or head is None or card is None:
                raise ValueError("confirmed strategy chain is incomplete")
            research_store = self._research_store or ResearchStore.default()
            source = _source_from_version(research_store, version)
            if source is None:
                raise ValueError("confirmed strategy source chain is incomplete")
            spec, payload = build_strategy_visualization(
                version=version,
                head=head,
                card=card,
                receipt=receipt,
                data_basis=_data_basis(source),
                head_confirmation_hash=receipt.confirmation_hash,
            )
            persist_strategy_visualization(run_dir, spec, payload)
        if self._event_callback is not None:
            self._event_callback(
                "strategy.confirmed",
                {
                    "version_id": receipt.version_id,
                    "receipt_id": receipt.receipt_id,
                    "visualization_id": spec.visualization_id,
                },
            )
        return json.dumps(
            {
                "status": "confirmed",
                "version_id": receipt.version_id,
                "receipt_id": receipt.receipt_id,
                "visualizations": [spec.model_dump(mode="json")],
                "worker_started": False,
            },
            ensure_ascii=False,
        )
