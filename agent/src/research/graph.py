"""Validation for complete, single-snapshot QE1 research object chains."""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import ResearchObject


class ResearchGraphError(ValueError):
    """Raised when a persisted research DAG is incomplete or inconsistent."""


def validate_research_chain(objects: Sequence[ResearchObject]) -> None:
    """Require a closed acyclic chain with one owner, spec, and snapshot."""

    by_id = {item.object_id: item for item in objects}
    if len(by_id) != len(objects):
        raise ResearchGraphError("object chain contains duplicate object IDs")
    if not by_id:
        raise ResearchGraphError("object chain is empty")
    owners = {item.owner_scope for item in objects}
    if len(owners) != 1:
        raise ResearchGraphError("object chain crosses owner_scope boundaries")
    specs = {item.object_id for item in objects if item.object_type == "research_spec"}
    snapshots = {item.object_id for item in objects if item.object_type == "data_snapshot_ref"}
    if len(specs) != 1:
        raise ResearchGraphError("object chain must contain exactly one research_spec")
    if len(snapshots) != 1:
        raise ResearchGraphError("object chain must contain exactly one data_snapshot_ref")

    for item in objects:
        for parent in item.parent_refs:
            stored = by_id.get(parent.object_id)
            if stored is None:
                raise ResearchGraphError(f"missing parent object: {parent.object_id}")
            if stored.ref() != parent:
                raise ResearchGraphError(f"parent reference does not match object: {parent.object_id}")

    visiting: set[str] = set()
    memo: dict[str, set[str]] = {}

    def ancestors(object_id: str) -> set[str]:
        if object_id in visiting:
            raise ResearchGraphError(f"object chain contains a cycle at {object_id}")
        if object_id in memo:
            return set(memo[object_id])
        visiting.add(object_id)
        result: set[str] = set()
        for parent in by_id[object_id].parent_refs:
            result.add(parent.object_id)
            result.update(ancestors(parent.object_id))
        visiting.remove(object_id)
        memo[object_id] = set(result)
        return set(result)

    spec_id = next(iter(specs))
    snapshot_id = next(iter(snapshots))
    for item in objects:
        ancestry = ancestors(item.object_id)
        if item.object_type != "research_spec" and spec_id not in ancestry:
            raise ResearchGraphError(f"{item.object_type} does not descend from the research_spec")
        if item.object_type not in {"research_spec", "data_snapshot_ref"} and snapshot_id not in ancestry:
            raise ResearchGraphError(f"{item.object_type} does not descend from the data_snapshot_ref")
