"""Attach a persisted QE3 similarity result to the current chat run."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.research.contracts import ResearchSpec, SimilarityRun, canonical_json
from src.research.similarity_presentation import (
    SimilarityVisualizationCandidate,
    SimilarityVisualizationPayload,
    SimilarityVisualizationSpec,
)
from src.research.store import ResearchStore
from src.tools.path_utils import safe_path, safe_run_dir

_STABILITY_RE = re.compile(
    r"^sensitivity_summary:([^:]+):.*(?:^|;)mean_rank_stability=([0-9]+(?:\.[0-9]+)?)"
)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _rank_stability(notes: tuple[str, ...]) -> dict[str, float]:
    result: dict[str, float] = {}
    for note in notes:
        match = _STABILITY_RE.match(note)
        if match:
            value = float(match.group(2))
            if 0.0 <= value <= 1.0:
                result[match.group(1)] = value
    return result


class ShowSimilarityResultTool(BaseTool):
    """Render an immutable similarity_run through the bounded chat UI contract."""

    name = "show_similarity_result"
    description = (
        "Attach a persisted QE3 similarity ranking to the chat. Pass only the "
        "content-addressed similarity_run_id returned by the research workflow; "
        "the tool verifies and loads the immutable object from the research store."
    )
    parameters = {
        "type": "object",
        "properties": {
            "similarity_run_id": {
                "type": "string",
                "pattern": r"^similarity_run:[0-9a-f]{64}$",
                "description": "Content-addressed QE3 similarity_run object ID.",
            },
            "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "required": ["similarity_run_id"],
    }
    repeatable = True
    is_readonly = False
    requires_current_run_dir = True

    def __init__(self, store: ResearchStore | None = None) -> None:
        self._store = store

    def execute(self, **kwargs: Any) -> str:
        similarity_run_id = str(kwargs.get("similarity_run_id") or "").strip()
        if not re.fullmatch(r"similarity_run:[0-9a-f]{64}", similarity_run_id):
            raise ValueError("similarity_run_id must be a canonical similarity_run object ID")
        top_n = kwargs.get("top_n", 10)
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 50:
            raise ValueError("top_n must be an integer between 1 and 50")
        title = str(kwargs.get("title") or "相似标的候选").strip()
        if not 1 <= len(title) <= 200:
            raise ValueError("title must contain 1 to 200 characters")

        run_dir_raw = str(kwargs.get("run_dir") or "").strip()
        if not run_dir_raw:
            raise ValueError("run_dir is required")
        run_dir = safe_run_dir(run_dir_raw)

        store = self._store or ResearchStore.default()
        similarity_object = store.get(similarity_run_id)
        if similarity_object is None or similarity_object.object_type != "similarity_run":
            raise ValueError("similarity_run was not found in the household research store")
        similarity = SimilarityRun.model_validate(similarity_object.payload)
        research_object = store.get(similarity.research_spec_ref.object_id)
        if research_object is None or research_object.object_type != "research_spec":
            raise ValueError("similarity_run references a missing research_spec")
        research_spec = ResearchSpec.model_validate(research_object.payload)

        selected = similarity.candidates[:top_n]
        stability = _rank_stability(similarity.sensitivity_notes)
        candidates = tuple(
            SimilarityVisualizationCandidate(
                rank=index,
                symbol=candidate.symbol,
                combined_score=candidate.combined_score,
                coverage=candidate.coverage,
                business_score=candidate.business_score,
                factor_score=candidate.factor_score,
                price_volume_score=candidate.price_volume_score,
                rank_stability=stability.get(candidate.symbol),
                evidence=candidate.evidence[:12],
                counterevidence=candidate.counterevidence[:12],
            )
            for index, candidate in enumerate(selected, start=1)
        )
        visualization_id = f"similarity_{similarity_object.content_sha256[:20]}_{len(candidates)}"
        payload = SimilarityVisualizationPayload(
            visualization_id=visualization_id,
            similarity_run_id=similarity_object.object_id,
            similarity_sha256=similarity_object.content_sha256,
            research_spec_id=research_object.object_id,
            target_symbols=research_spec.symbols,
            as_of=research_spec.as_of,
            candidate_universe=research_spec.candidate_universe,
            weights=similarity.weights,
            candidates=candidates,
            excluded_symbol_count=len(similarity.excluded_symbols),
        )
        spec = SimilarityVisualizationSpec(
            visualization_id=visualization_id,
            data_ref=visualization_id,
            title=title,
            similarity_run_id=similarity_object.object_id,
            similarity_sha256=similarity_object.content_sha256,
            target_symbols=research_spec.symbols,
            as_of=research_spec.as_of,
            candidate_universe=research_spec.candidate_universe,
            candidate_count=len(candidates),
            weights=similarity.weights,
            fallback_text=f"{len(candidates)} similarity candidates for {', '.join(research_spec.symbols)}",
        )

        output_path = safe_path(f"artifacts/visualizations/{visualization_id}.json", run_dir)
        manifest_path = safe_path("artifacts/visualizations.json", run_dir)
        manifest: list[dict[str, Any]] = []
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                manifest = [item for item in existing if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            pass

        payload_json = payload.model_dump(mode="json")
        spec_json = spec.model_dump(mode="json")
        _atomic_write_json(output_path, payload_json)
        manifest = [item for item in manifest if item.get("visualization_id") != visualization_id]
        manifest.append(spec_json)
        _atomic_write_json(manifest_path, manifest[-20:])
        return json.dumps(
            {
                "status": "ok",
                "similarity_run_id": similarity_object.object_id,
                "candidate_count": len(candidates),
                "visualizations": [spec_json],
                "message": "Similarity ranking attached to the chat response.",
            },
            ensure_ascii=False,
            allow_nan=False,
        )
