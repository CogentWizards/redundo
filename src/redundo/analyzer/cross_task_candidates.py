"""Candidate pairs whose two sides sit in *different* tasks: a real
cross-run or cross-agent repeat, not a same-task lineage repeat.
cycles.py and near_duplicates.py own the same-task case entirely,
unchanged; this module never reconsiders a repeat they've already
claimed (see `already_claimed` below), and never pairs two events from
the same task_id, by construction, that would be re-litigating a
decision those two modules already made correctly (including their own
"normal fan-out, not waste" exclusion for real parallel siblings).

Two matching regimes, same as the rest of this package: exact
content_hash equality, and SimHash near-duplicate similarity. Both scan
the *whole* corpus, not one task's own lineage, which is the entire
point (see docs on cross-run fragmentation and cross-agent redundancy).
Exact match stays a simple O(n) hash-group scan. Near-duplicate compares
every pair within one (event_type, name) group exactly, as long as the
group is small enough for that to be cheap, and only falls back to LSH
banding past that size. See `_EXHAUSTIVE_GROUP_LIMIT` for why banding
can't be the default at this threshold.

"Nearest, not every" still applies, generalized from "nearest lineage
ancestor" to "nearest-in-time predecessor from a different task": the
question this whole package asks is always "did anything change since
the most recent relevant occurrence," and only the most recent
occurrence can answer it. An event with no timestamp is excluded
entirely rather than guessed into an order. See schema.py, timestamp
is the one field this module cannot degrade around.

This module says nothing about whether a found pair is meaningful: it
might connect two genuinely related tasks (a confirmed delegation link,
see task_graph.py) or be pure content coincidence between two unrelated
tasks. Bucketing that decision belongs to whatever calls this (see
analyses/waste.py's cross_task_redundancy/recurring_pattern split), not
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cycles import CandidatePair
from .near_duplicates import DEFAULT_SIMILARITY_THRESHOLD, NearDuplicatePair
from .schema import Event

_CALL_TYPES = ("llm_call", "tool_call")

# Near-duplicate search compares each event against every earlier event
# in its (event_type, name) group, newest first, up to this many back.
# Banding alone can't do this job at the default threshold: 4 exact
# bands of 16 bits only guarantee a shared band when distance < 4, and a
# pair at distance d shares no band with probability
#   sum_k (-1)^k C(4,k) C(64-16k, d) / C(64, d)
# which is 10% at d=4, 42% at d=6, 67% at d=8 and 99.95% at d=24. On real
# captures that dropped every cross-task near match. Narrower bands don't
# rescue it, since any banding selective enough to save work misses most
# pairs near 24 bits. The window keeps the cost linear in group size (at
# most this many XOR/popcounts per event), and each scan stops at the
# first, i.e. nearest-in-time, qualifying predecessor.
_EXHAUSTIVE_WINDOW = 4096

# Events older than the window are still reachable through the old 4 x
# 16-bit banding, so a very close match far back in a huge group is not
# lost: nothing the banded search used to find is missed now.
_BAND_COUNT = 4
_BAND_BITS = 64 // _BAND_COUNT


@dataclass(frozen=True, slots=True)
class CrossTaskPair:
    task_id: str  # the repeat's own task_id, for reporting symmetry with CandidatePair
    original: Event
    repeat: Event
    hamming_distance: int  # 0 for an exact match


def find_cross_task_pairs(
    events: list[Event],
    *,
    exact_pairs: list[CandidatePair] | None = None,
    near_pairs: list[NearDuplicatePair] | None = None,
    threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[CrossTaskPair]:
    """exact_pairs/near_pairs: the already-computed same-task results (see
    cycles.find_candidate_pairs / near_duplicates.find_near_duplicate_pairs).
    Every event that's already a `.repeat` in either is excluded up front,
    the same non-overlap discipline near_duplicates.py already applies
    against cycles.py.
    """
    already_claimed = {
        (p.repeat.task_id, p.repeat.step_index) for p in (exact_pairs or ())
    } | {
        (p.repeat.task_id, p.repeat.step_index) for p in (near_pairs or ())
    }

    candidates = [
        e
        for e in events
        if e.event_type in _CALL_TYPES
        and e.timestamp is not None
        and (e.task_id, e.step_index) not in already_claimed
    ]

    pairs = _exact_cross_task_pairs(candidates)
    claimed_here = {(p.repeat.task_id, p.repeat.step_index) for p in pairs}
    remaining = [c for c in candidates if (c.task_id, c.step_index) not in claimed_here]
    pairs.extend(_near_cross_task_pairs(remaining, threshold))
    return pairs


def _nearest_different_task_predecessor(group: list[Event]) -> dict[tuple[str, int], Event]:
    """group: events sharing an exact signature, any task, unordered.
    Every occurrence within one group is an equally valid match (that's
    what "exact" means), so the only question is recency: each event's
    predecessor is whichever *other* task's most-recently-seen entry is
    itself most recent. Single forward pass in time order, tracking only
    the latest entry per task. Ties (identical timestamp) broken by
    task_id for deterministic output.
    """
    ordered = sorted(group, key=lambda e: (e.timestamp, e.task_id, e.step_index))
    last_by_task: dict[str, Event] = {}
    predecessor: dict[tuple[str, int], Event] = {}

    for event in ordered:
        others = [e for tid, e in last_by_task.items() if tid != event.task_id]
        if others:
            predecessor[(event.task_id, event.step_index)] = max(
                others, key=lambda e: (e.timestamp, e.task_id)
            )
        last_by_task[event.task_id] = event

    return predecessor


def _exact_cross_task_pairs(candidates: list[Event]) -> list[CrossTaskPair]:
    groups: dict[tuple[str, str, str], list[Event]] = {}
    for event in candidates:
        key = (event.event_type, event.name, event.content_hash)
        groups.setdefault(key, []).append(event)

    pairs: list[CrossTaskPair] = []
    for group in groups.values():
        if len({e.task_id for e in group}) < 2:
            continue  # every occurrence is the same task, not this module's job
        for (task_id, step_index), predecessor in _nearest_different_task_predecessor(group).items():
            repeat = next(e for e in group if (e.task_id, e.step_index) == (task_id, step_index))
            pairs.append(
                CrossTaskPair(
                    task_id=task_id, original=predecessor, repeat=repeat, hamming_distance=0,
                )
            )
    return pairs


def _bands(value: int) -> tuple[int, ...]:
    mask = (1 << _BAND_BITS) - 1
    return tuple((value >> (i * _BAND_BITS)) & mask for i in range(_BAND_COUNT))


def _fingerprint_of(event: Event) -> str | None:
    if not isinstance(event.metadata, dict):
        return None
    fingerprint = event.metadata.get("similarity_fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _nearest_qualifying_predecessors(
    group: list[tuple[Event, int]], threshold: int,
) -> list[tuple[Event, Event, int]]:
    """group: (event, fingerprint) pairs sharing one (event_type, name),
    any task. Returns (repeat, original, distance) for every event with a
    different-task predecessor within `threshold`, picking the nearest
    in time. Ties (identical timestamp) go to the later task_id, the same
    rule _nearest_different_task_predecessor uses.

    Predecessors inside the last _EXHAUSTIVE_WINDOW events are all
    checked, newest first. Only if none qualifies are the older ones
    reached through the band index, which is filled as events slide out
    of the window, so it never holds anything the window already covers.
    """
    ordered = sorted(group, key=lambda p: (p[0].timestamp, p[0].task_id, p[0].step_index))
    band_index: dict[tuple[int, int], list[int]] = {}
    results: list[tuple[Event, Event, int]] = []

    for i, (event, fp) in enumerate(ordered):
        evicted = i - _EXHAUSTIVE_WINDOW - 1
        if evicted >= 0:
            for band in enumerate(_bands(ordered[evicted][1])):
                band_index.setdefault(band, []).append(evicted)

        match: tuple[int, int] | None = None  # (index into ordered, distance)
        for j in range(i - 1, max(evicted, -1), -1):
            candidate, candidate_fp = ordered[j]
            if candidate.task_id == event.task_id:
                continue
            distance = (fp ^ candidate_fp).bit_count()
            if distance <= threshold:
                match = (j, distance)
                break

        if match is None and band_index:
            for band in enumerate(_bands(fp)):
                for j in reversed(band_index.get(band, ())):
                    if match is not None and j <= match[0]:
                        break
                    candidate, candidate_fp = ordered[j]
                    if candidate.task_id == event.task_id:
                        continue
                    distance = (fp ^ candidate_fp).bit_count()
                    if distance <= threshold:
                        match = (j, distance)
                        break

        if match is not None:
            results.append((event, ordered[match[0]][0], match[1]))

    return results


def _near_cross_task_pairs(candidates: list[Event], threshold: int) -> list[CrossTaskPair]:
    groups: dict[tuple[str, str], list[tuple[Event, int]]] = {}
    for event in candidates:
        fingerprint = _fingerprint_of(event)
        if fingerprint is None:
            continue
        groups.setdefault((event.event_type, event.name), []).append(
            (event, int(fingerprint, 16))
        )

    pairs: list[CrossTaskPair] = []
    for group in groups.values():
        if len({e.task_id for e, _ in group}) < 2:
            continue
        for repeat, original, distance in _nearest_qualifying_predecessors(group, threshold):
            pairs.append(
                CrossTaskPair(
                    task_id=repeat.task_id,
                    original=original,
                    repeat=repeat,
                    hamming_distance=distance,
                )
            )
    return pairs
