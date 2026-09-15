"""Cross-run context drift: a hint, deliberately not a finding.

Everything else in this package (WasteAnalysis's buckets, included the
cross-task ones) is a deterministic, reproducible claim against a
threshold that's at least been checked against a real capture (see
near_duplicates.DEFAULT_SIMILARITY_THRESHOLD's own docstring). This
module has no equivalent evidence behind it: no source today reports a
real "continuation" link (see task_graph.py's module docstring on why
that's a different relationship than the "delegation" link
hermes-otel does report), so nothing here has been calibrated against
real drift. It intentionally does not classify anything as a bucket,
a verdict, or even a threshold pass/fail. It reports measured distances
plainly and lets a human decide what's worth a look, the way this
module's own CLI output says so explicitly.

Two independent axes, never blended into one score, matching this
project's standing discipline against conflating differently-shaped
findings (see near_duplicate vs. the three exact-match buckets, or
cross_task_redundancy vs. recurring_pattern):

- shape distance: edit distance between two tasks' own ordered call
  signatures ((event_type, name), in step order). Catches "the workflow
  now takes a different path," independent of what any one call's
  content looks like.
- content distance: SimHash Hamming distance, computed only at the
  positions the alignment says actually correspond between the two
  tasks (never across a substitution, comparing a tool_call's
  fingerprint to an llm_call's would not be a meaningful signal, the
  same restriction near_duplicates.py already applies within one task).

The alignment itself is the more useful output than either number alone:
it names the specific calls added, removed, or substituted between one
task and the next, a literal, spot-checkable diff, not a distance
someone has to interpret on faith.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapter.hashing import hamming_distance
from .schema import Event
from .task_graph import TaskGraph, build_task_graph

_CALL_TYPES = ("llm_call", "tool_call")


@dataclass(frozen=True, slots=True)
class DriftHint:
    predecessor_task_id: str
    successor_task_id: str
    predecessor_call_count: int
    successor_call_count: int
    shape_distance: int  # raw edit distance
    shape_distance_fraction: float  # normalized by the longer sequence's length
    # (predecessor_step_index, successor_step_index, hamming_distance) for
    # every aligned pair with a matching signature and a fingerprint on
    # both sides. Never populated across a substitution.
    content_distances: tuple[tuple[int, int, int], ...]
    # (event_type, name, step_index) calls present in the successor with
    # no counterpart in the predecessor.
    added: tuple[tuple[str, str, int], ...]
    # (event_type, name, step_index) calls present in the predecessor
    # with no counterpart in the successor.
    removed: tuple[tuple[str, str, int], ...]
    # ((predecessor_type, predecessor_name, predecessor_step),
    #  (successor_type, successor_name, successor_step)) pairs the
    # alignment matched positionally but whose signature differs, a
    # different kind of call took this step's place.
    substituted: tuple[tuple[tuple[str, str, int], tuple[str, str, int]], ...]


def find_drift_hints(events: list[Event], *, graph: TaskGraph | None = None) -> list[DriftHint]:
    """One DriftHint per consecutive hop in every continuation chain of
    more than one task. A single-task chain has nothing to compare
    against and produces nothing. No threshold gating: every hop is
    reported, with its real measured distances, never hidden behind an
    uncalibrated cutoff presented as meaningful, see module docstring.
    """
    if graph is None:
        graph = build_task_graph(events)

    events_by_task: dict[str, list[Event]] = {}
    for event in events:
        events_by_task.setdefault(event.task_id, []).append(event)

    hints: list[DriftHint] = []
    for chain in find_continuation_chains(graph):
        for predecessor_id, successor_id in zip(chain, chain[1:]):
            predecessor_seq = _call_sequence(events_by_task.get(predecessor_id, []))
            successor_seq = _call_sequence(events_by_task.get(successor_id, []))
            hints.append(_compare(predecessor_id, successor_id, predecessor_seq, successor_seq))
    return hints


def find_continuation_chains(graph: TaskGraph) -> list[list[str]]:
    """Every maximal continuation chain, root-to-latest order, length 2 or
    more only: a task with no continuation edge at all never enters
    `all_chain_tasks` below, so there's nothing to walk for it. Each
    task has at most one continuation parent, so chains are disjoint
    linear paths; walking from every "tip" (a task nothing continues
    from) once covers the whole graph without double-reporting a hop.
    """
    has_a_successor = {p for p in graph.continuation_parent_of.values() if p is not None}
    all_chain_tasks: set[str] = set(graph.continuation_parent_of.keys()) | has_a_successor
    tips = all_chain_tasks - has_a_successor

    chains = []
    for tip in tips:
        chains.append(_chain_of(graph, tip))
    return chains


def _chain_of(graph: TaskGraph, task_id: str) -> list[str]:
    """Root-to-latest order, walking continuation_parent_of backward from
    `task_id`. Cycle guard: a malformed graph stops rather than loops
    forever, the same discipline lineage.py's ancestors_of already
    applies to a cyclic parent_id chain.
    """
    chain = [task_id]
    seen = {task_id}
    current = graph.continuation_parent_of.get(task_id)
    while current is not None:
        if current in seen:
            break
        chain.append(current)
        seen.add(current)
        current = graph.continuation_parent_of.get(current)
    chain.reverse()
    return chain


def _call_sequence(task_events: list[Event]) -> list[Event]:
    calls = [e for e in task_events if e.event_type in _CALL_TYPES]
    return sorted(calls, key=lambda e: e.step_index)


def _signature(event: Event) -> tuple[str, str]:
    return (event.event_type, event.name)


def _fingerprint_of(event: Event) -> str | None:
    if not isinstance(event.metadata, dict):
        return None
    fingerprint = event.metadata.get("similarity_fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _align(seq_a: list[Event], seq_b: list[Event]) -> tuple[int, list[tuple[int | None, int | None]]]:
    """Standard Wagner-Fischer global alignment (edit distance with
    traceback) over the two sequences' (event_type, name) signatures.
    Match cost 0 for an identical signature, 1 for a substitution or an
    insertion/deletion. Returns (edit_distance, alignment), alignment is
    a list of (index_in_a_or_None, index_in_b_or_None) pairs in order.
    """
    n, m = len(seq_a), len(seq_b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            substitution_cost = 0 if _signature(seq_a[i - 1]) == _signature(seq_b[j - 1]) else 1
            dp[i][j] = min(
                dp[i - 1][j - 1] + substitution_cost,
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
            )

    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            substitution_cost = 0 if _signature(seq_a[i - 1]) == _signature(seq_b[j - 1]) else 1
            if dp[i][j] == dp[i - 1][j - 1] + substitution_cost:
                pairs.append((i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            pairs.append((i - 1, None))
            i -= 1
            continue
        pairs.append((None, j - 1))
        j -= 1

    pairs.reverse()
    return dp[n][m], pairs


def _compare(
    predecessor_id: str,
    successor_id: str,
    predecessor_seq: list[Event],
    successor_seq: list[Event],
) -> DriftHint:
    distance, alignment = _align(predecessor_seq, successor_seq)
    longest = max(len(predecessor_seq), len(successor_seq), 1)

    content_distances: list[tuple[int, int, int]] = []
    added: list[tuple[str, str, int]] = []
    removed: list[tuple[str, str, int]] = []
    substituted: list[tuple[tuple[str, str, int], tuple[str, str, int]]] = []

    for i, j in alignment:
        if i is not None and j is not None:
            pred_event, succ_event = predecessor_seq[i], successor_seq[j]
            if _signature(pred_event) == _signature(succ_event):
                pred_fp, succ_fp = _fingerprint_of(pred_event), _fingerprint_of(succ_event)
                if pred_fp is not None and succ_fp is not None:
                    content_distances.append((
                        pred_event.step_index, succ_event.step_index,
                        hamming_distance(pred_fp, succ_fp),
                    ))
            else:
                substituted.append((
                    (pred_event.event_type, pred_event.name, pred_event.step_index),
                    (succ_event.event_type, succ_event.name, succ_event.step_index),
                ))
        elif j is not None:
            succ_event = successor_seq[j]
            added.append((succ_event.event_type, succ_event.name, succ_event.step_index))
        elif i is not None:
            pred_event = predecessor_seq[i]
            removed.append((pred_event.event_type, pred_event.name, pred_event.step_index))

    return DriftHint(
        predecessor_task_id=predecessor_id,
        successor_task_id=successor_id,
        predecessor_call_count=len(predecessor_seq),
        successor_call_count=len(successor_seq),
        shape_distance=distance,
        shape_distance_fraction=distance / longest,
        content_distances=tuple(content_distances),
        added=tuple(added),
        removed=tuple(removed),
        substituted=tuple(substituted),
    )
