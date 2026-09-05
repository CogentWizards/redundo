"""Branch-aware structure over a task's events.

The contract says parent_id is "the id of the event that produced/spawned
this one," scoped to (task_id, step_index). Two assumptions follow that
aren't spelled out in the schema itself -- documented here so they're
inspectable rather than buried in cycle-detection logic:

1. A source that never populates parent_id is assumed to be a single linear
   thread of execution: each event's effective parent is simply the
   immediately preceding event (by step_index) in the same task. This makes
   the tool useful on flat traces (most harnesses, including our own,
   produce these) without requiring branch instrumentation up front.

2. "Redundant repeat" is a lineage-relative notion. Two identical calls
   count as a candidate cycle only if one is an ancestor of the other along
   the parent chain -- i.e. the same execution path revisited the same
   call. Two sibling branches that happen to make the same call are not a
   cycle; that's normal fan-out, not waste.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .schema import Event


@dataclass(frozen=True, slots=True)
class TaskLineage:
    """Per-task index: events in order, effective-parent map, ancestor lookups."""

    task_id: str
    events: tuple[Event, ...]  # sorted by step_index
    _by_step: dict[int, Event]
    _effective_parent_step: dict[int, int | None]

    @classmethod
    def build(cls, task_events: list[Event]) -> "TaskLineage":
        if not task_events:
            raise ValueError("build() requires at least one event")
        task_id = task_events[0].task_id
        if any(e.task_id != task_id for e in task_events):
            raise ValueError("all events must share the same task_id")

        events = tuple(sorted(task_events, key=lambda e: e.step_index))
        by_step = {e.step_index: e for e in events}

        effective_parent: dict[int, int | None] = {}
        previous_step: int | None = None
        for event in events:
            if event.parent_id is not None and event.parent_id in by_step:
                effective_parent[event.step_index] = event.parent_id
            else:
                # No usable parent_id: fall back to linear-thread assumption.
                effective_parent[event.step_index] = previous_step
            previous_step = event.step_index

        return cls(
            task_id=task_id,
            events=events,
            _by_step=by_step,
            _effective_parent_step=effective_parent,
        )

    def parent_of(self, event: Event) -> Event | None:
        parent_step = self._effective_parent_step.get(event.step_index)
        if parent_step is None:
            return None
        return self._by_step.get(parent_step)

    def ancestors_of(self, event: Event) -> Iterator[Event]:
        """Yield ancestors nearest-first, walking the effective-parent chain."""
        current = self.parent_of(event)
        seen: set[int] = {event.step_index}
        while current is not None:
            if current.step_index in seen:
                # A malformed/cyclic parent_id graph -- stop rather than loop
                # forever. This shouldn't happen with well-formed input.
                break
            seen.add(current.step_index)
            yield current
            current = self.parent_of(current)

    def children_of(self, event: Event) -> Iterator[Event]:
        for other in self.events:
            if self._effective_parent_step.get(other.step_index) == event.step_index:
                yield other

    def path_between(self, ancestor: Event, descendant: Event) -> list[Event]:
        """Events strictly between ancestor and descendant on the chain,
        nearest-to-descendant first. Assumes ancestor really is an ancestor
        of descendant (as returned by ancestors_of); callers should check.
        """
        between: list[Event] = []
        for node in self.ancestors_of(descendant):
            if node.step_index == ancestor.step_index:
                break
            between.append(node)
        return between

    @property
    def terminal_outcome(self) -> str | None:
        """The outcome of the task's last event by step_index, or None if
        that event has no outcome recorded. This is a task-wide proxy, not
        branch-precise -- see module docstring and README for why that's
        an acceptable default for a first pass, and a documented limitation
        for genuinely parallel/multi-branch tasks.
        """
        return self.events[-1].outcome


def group_by_task(events: list[Event]) -> dict[str, TaskLineage]:
    """Split a flat event list into one TaskLineage per task_id."""
    by_task: dict[str, list[Event]] = {}
    for event in events:
        by_task.setdefault(event.task_id, []).append(event)
    return {task_id: TaskLineage.build(task_events) for task_id, task_events in by_task.items()}
