"""A task-level graph over the whole corpus, built only from
metadata.parent_task_id (see schema.py's META_PARENT_TASK_ID_KEY), a
real, source-confirmed delegation link, never inferred from timing or
content similarity. This is what lets cross-task candidate detection
(cross_task_candidates.py) tell "these two tasks are actually related"
from "these two tasks just happen to share some content," without ever
guessing a relationship a source didn't report.

A task with no parent_task_id anywhere in its own events is its own
singleton component, the common case for most corpora today, since
only one source (hermes-otel, via Hermes's own subagent delegation)
currently populates this key at all. That's not a degraded case; it's
the honest default until more sources expose an equivalent link.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import Event


@dataclass(frozen=True, slots=True)
class TaskGraph:
    """parent_of: task_id -> the task_id it was reported delegated from, or
    None. component_of: task_id -> an opaque connected-component id; two
    tasks share a component id if and only if a chain of real
    parent_task_id links connects them (directly or transitively).
    """

    parent_of: dict[str, str | None]
    component_of: dict[str, int]


def build_task_graph(events: list[Event]) -> TaskGraph:
    parent_of: dict[str, str | None] = {}
    ambiguous: set[str] = set()

    for event in events:
        task_id = event.task_id
        if task_id not in parent_of:
            parent_of[task_id] = None
        if not isinstance(event.metadata, dict):
            continue
        reported = event.metadata.get("parent_task_id")
        if reported is None:
            continue
        if task_id in ambiguous:
            continue
        current = parent_of.get(task_id)
        if current is None:
            parent_of[task_id] = reported
        elif current != reported:
            # This task's own events disagree on their parent, the same
            # "ambiguous, don't guess" rule sources.openinference's
            # _resolve_task_id already applies to conflicting
            # gen_ai.conversation.id/session.id values. Never pick one of
            # the conflicting values; treat the link as absent instead.
            ambiguous.add(task_id)
            parent_of[task_id] = None

    component_of = _connected_components(parent_of)
    return TaskGraph(parent_of=parent_of, component_of=component_of)


def _connected_components(parent_of: dict[str, str | None]) -> dict[str, int]:
    """Union-find over the parent_task_id edges. A reported parent_task_id
    that never appears as its own task_id anywhere in the corpus (the
    parent task's own events weren't captured, or belong to a source that
    isn't adapted at all) still gets its own node here. The edge is
    real even if that side of it produced no events of its own, so it
    still merges whatever *does* reference it into one component.
    """
    node_id: dict[str, int] = {}

    def find(task_id: str) -> int:
        if task_id not in node_id:
            node_id[task_id] = len(node_id)
        return node_id[task_id]

    parent_ptr: list[int] = []

    def root(idx: int) -> int:
        while parent_ptr[idx] != idx:
            parent_ptr[idx] = parent_ptr[parent_ptr[idx]]
            idx = parent_ptr[idx]
        return idx

    def ensure(task_id: str) -> int:
        idx = find(task_id)
        while len(parent_ptr) <= idx:
            parent_ptr.append(len(parent_ptr))
        return idx

    for task_id, parent_task_id in parent_of.items():
        ensure(task_id)
        if parent_task_id is not None:
            a = root(ensure(task_id))
            b = root(ensure(parent_task_id))
            if a != b:
                parent_ptr[a] = b

    return {task_id: root(idx) for task_id, idx in node_id.items()}


def same_component(graph: TaskGraph, task_a: str, task_b: str) -> bool:
    """True only when a real parent_task_id chain connects the two tasks
    (directly or transitively). False for two unrelated tasks, and False
    for a task no event in the corpus ever mentioned at all (never a
    guess in either direction).
    """
    a = graph.component_of.get(task_a)
    b = graph.component_of.get(task_b)
    return a is not None and a == b
