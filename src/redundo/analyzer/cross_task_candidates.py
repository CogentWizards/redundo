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
Exact match stays a simple O(n) hash-group scan. Near-duplicate uses LSH
banding (the fingerprint split into fixed-width bands, candidates only
compared within a shared band) so it doesn't degenerate into an
all-pairs Hamming-distance scan at corpus scale. The standard trick
that makes SimHash search practical past a single small group.

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

from ..adapter.hashing import hamming_distance
from .cycles import CandidatePair
from .near_duplicates import DEFAULT_SIMILARITY_THRESHOLD, NearDuplicatePair
from .schema import Event

_CALL_TYPES = ("llm_call", "tool_call")

# The 64-bit fingerprint split into 4 non-overlapping 16-bit bands. A
# real near-duplicate is highly likely to collide in at least one band
# even when it doesn't collide in all four; this is what keeps the
# search sub-quadratic without missing genuine near-matches. More bands
# means smaller (cheaper) buckets but a slightly higher chance a real
# near-duplicate never shares one; 4 is a reasonable starting point, not
# a validated constant.
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


def _bands(fingerprint: str) -> tuple[int, ...]:
    value = int(fingerprint, 16)
    mask = (1 << _BAND_BITS) - 1
    return tuple((value >> (i * _BAND_BITS)) & mask for i in range(_BAND_COUNT))


def _fingerprint_of(event: Event) -> str | None:
    if not isinstance(event.metadata, dict):
        return None
    fingerprint = event.metadata.get("similarity_fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _nearest_qualifying_predecessor(
    group: list[Event],
    fingerprints: dict[tuple[str, int], str],
    threshold: int,
) -> dict[tuple[str, int], Event]:
    """Like _nearest_different_task_predecessor, but a band collision only
    makes a pair cheap enough to *check*, it doesn't mean the pair is
    actually within `threshold`. Every earlier, different-task candidate
    in the bucket is checked against the real Hamming distance; among the
    ones that pass, the nearest in time wins. O(bucket_size^2) worst
    case, fine here since LSH banding is what keeps buckets themselves
    small, not this step.
    """
    ordered = sorted(group, key=lambda e: (e.timestamp, e.task_id, e.step_index))
    seen: list[Event] = []
    predecessor: dict[tuple[str, int], Event] = {}

    for event in ordered:
        key = (event.task_id, event.step_index)
        fp = fingerprints[key]
        best: Event | None = None
        for candidate in seen:
            if candidate.task_id == event.task_id:
                continue
            candidate_fp = fingerprints[(candidate.task_id, candidate.step_index)]
            if hamming_distance(fp, candidate_fp) > threshold:
                continue
            if best is None or candidate.timestamp > best.timestamp:
                best = candidate
        if best is not None:
            predecessor[key] = best
        seen.append(event)

    return predecessor


def _near_cross_task_pairs(candidates: list[Event], threshold: int) -> list[CrossTaskPair]:
    fingerprints: dict[tuple[str, int], str] = {}
    buckets: dict[tuple[str, str, int, int], list[Event]] = {}
    for event in candidates:
        fingerprint = _fingerprint_of(event)
        if fingerprint is None:
            continue
        fingerprints[(event.task_id, event.step_index)] = fingerprint
        # An event lands in up to _BAND_COUNT buckets, one per band, so a
        # genuine near-duplicate only needs to collide in one of them to
        # be found at all.
        for band_idx, band_value in enumerate(_bands(fingerprint)):
            buckets.setdefault((event.event_type, event.name, band_idx, band_value), []).append(event)

    # An (original, repeat) pair may collide in more than one band, so
    # keep only the single best (nearest-in-time, threshold-qualifying)
    # predecessor per repeat across every bucket it appears in.
    best_predecessor: dict[tuple[str, int], Event] = {}
    for group in buckets.values():
        if len({e.task_id for e in group}) < 2:
            continue
        for key, predecessor in _nearest_qualifying_predecessor(group, fingerprints, threshold).items():
            current_best = best_predecessor.get(key)
            if current_best is None or predecessor.timestamp > current_best.timestamp:
                best_predecessor[key] = predecessor

    pairs: list[CrossTaskPair] = []
    for (task_id, step_index), predecessor in best_predecessor.items():
        repeat_fp = fingerprints[(task_id, step_index)]
        pred_fp = fingerprints[(predecessor.task_id, predecessor.step_index)]
        repeat = next(
            e for e in candidates if (e.task_id, e.step_index) == (task_id, step_index)
        )
        pairs.append(
            CrossTaskPair(
                task_id=task_id,
                original=predecessor,
                repeat=repeat,
                hamming_distance=hamming_distance(repeat_fp, pred_fp),
            )
        )
    return pairs
