"""Find candidate near-duplicate pairs -- calls that are similar, not
identical, to an earlier call in the same execution path.

This is a separate module from cycles.py, not a variant inside it:
cycles.py's exact-match path is well-tested and load-bearing for the
three existing (confirmed_waste/likely_legitimate/unclassified) buckets,
and this module's fuzzy-matching logic has no business risking a
regression there.

Non-overlap with cycles.py's CandidatePairs is enforced structurally, not
by trying to make the two searches agree pairwise: `find_near_duplicate_pairs`
takes the already-computed exact pairs and excludes every event that's
already a `.repeat` there, before this module's own ancestor walk ever
runs for it. This matters more than it looks like it should -- cycles.py's
walk and this module's walk use different equality criteria (full
(event_type, name, content_hash) vs. (event_type, name) plus a Hamming
threshold), so they don't necessarily agree on which ancestor is
"nearest" for a given repeat event. Trying to reconcile that by checking,
inside this module's own walk, whether the nearest same-(type, name)
ancestor it finds happens to also be an exact hash match is NOT
sufficient on its own in a 3+-call chain -- see the walk's own docstring
for the specific case. Excluding already-claimed repeats up front sidesteps
the disagreement entirely: an event can be the *ancestor* referenced by
both an exact pair and a near-duplicate pair (normal, same as a 3-call
identical chain making one event both a repeat and an original in
cycles.py alone), but it is never the classified *repeat* in more than
one bucket.

Only ever computed over the *call* side of an event (llm_call/tool_call),
via metadata["similarity_fingerprint"] -- never over tool_result/response
content, which is out of scope for this analysis (see
hashing.similarity_fingerprint's own docstring for where it's populated).
A record missing a fingerprint (a source that doesn't populate this
metadata key yet) never produces a false match -- it's simply excluded
from consideration, same "absence means unknown, never a guess"
discipline as everywhere else in this package.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapter.hashing import hamming_distance
from .cycles import CandidatePair, find_candidate_pairs
from .lineage import TaskLineage, group_by_task
from .schema import Event

# Bits out of 64 (see hashing.SIMHASH_BITS). A starting point calibrated
# against one real capture (a near-identical URL differing by one query
# param landed at 8; a genuinely unrelated pair landed at 34), not a
# validated constant -- override via
# WasteAnalysis(near_duplicate_threshold=...). Biased conservative (fewer
# false positives), matching this codebase's stated preference elsewhere
# (hashing.mask_volatile's mask_integers docstring).
DEFAULT_SIMILARITY_THRESHOLD = 24


@dataclass(frozen=True, slots=True)
class NearDuplicatePair:
    task_id: str
    original: Event
    repeat: Event
    intervening: tuple[Event, ...]
    hamming_distance: int


def find_near_duplicate_pairs(
    events: list[Event],
    *,
    lineages: dict[str, TaskLineage] | None = None,
    exact_pairs: list[CandidatePair] | None = None,
    threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[NearDuplicatePair]:
    """One pair per event whose nearest same-(event_type, name) ancestor
    is similar (not identical) within `threshold` Hamming-distance bits.
    Mirrors find_candidate_pairs's "nearest, not every" and "each event is
    the repeat at most once" discipline -- see cycles.py's module
    docstring for the full reasoning, which applies unchanged here.

    `exact_pairs`: pass an already-computed `find_candidate_pairs(...)`
    result if the caller needs it for anything else too (e.g. WasteAnalysis
    already computes it for the three existing buckets) -- avoids
    computing it twice. Computed fresh when omitted. Either way, every
    event that's already a `.repeat` in `exact_pairs` is excluded from
    this search entirely -- see module docstring for why.
    """
    if lineages is None:
        lineages = group_by_task(events)
    if exact_pairs is None:
        exact_pairs = find_candidate_pairs(events, lineages=lineages)
    already_exact_repeat = {(p.repeat.task_id, p.repeat.step_index) for p in exact_pairs}

    pairs: list[NearDuplicatePair] = []
    for lineage in lineages.values():
        pairs.extend(_pairs_for_task(lineage, threshold, already_exact_repeat))
    return pairs


def _pairs_for_task(
    lineage: TaskLineage,
    threshold: int,
    already_exact_repeat: set[tuple[str, int]],
) -> list[NearDuplicatePair]:
    pairs: list[NearDuplicatePair] = []
    for event in lineage.events:
        if event.event_type not in ("llm_call", "tool_call"):
            continue
        if (event.task_id, event.step_index) in already_exact_repeat:
            continue  # already classified via cycles.py -- not this bucket's territory
        match = _nearest_similar_ancestor(lineage, event, threshold)
        if match is None:
            continue
        ancestor, distance = match
        intervening = tuple(reversed(lineage.path_between(ancestor, event)))
        pairs.append(
            NearDuplicatePair(
                task_id=lineage.task_id,
                original=ancestor,
                repeat=event,
                intervening=intervening,
                hamming_distance=distance,
            )
        )
    return pairs


def _fingerprint_of(event: Event) -> str | None:
    if not isinstance(event.metadata, dict):
        return None
    fingerprint = event.metadata.get("similarity_fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _nearest_similar_ancestor(
    lineage: TaskLineage, event: Event, threshold: int
) -> tuple[Event, int] | None:
    """Walk ancestors nearest-first looking for the first one that shares
    (event_type, name) with `event` -- the same weak signature cycles.py
    checks before comparing content_hash -- and stop there, whatever the
    outcome. Not "keep walking until something similar enough turns up":
    a farther, more-similar ancestor existing further up the chain is
    intentionally left unpaired, same reasoning as cycles.py's own
    "nearest, not every" rule (the relevant question is "did anything
    change since the last time this specific call shape happened," which
    only the nearest occurrence can answer).

    Because the caller has already excluded every event that's a `.repeat`
    in an exact CandidatePair, the nearest same-(type, name) ancestor found
    here is provably never an exact content_hash match either -- if it
    were, cycles.py's own walk (which uses the identical nearest-first
    ancestor order, just checking one more field) would have found that
    same ancestor first and this event would already be excluded. The
    explicit check below is kept anyway as a direct, self-evident guard
    rather than relying on that cross-module proof silently holding.
    """
    fingerprint = _fingerprint_of(event)
    if fingerprint is None:
        return None
    for ancestor in lineage.ancestors_of(event):
        if ancestor.event_type != event.event_type or ancestor.name != event.name:
            continue
        if ancestor.content_hash == event.content_hash:
            return None  # exact match -- cycles.py's territory; see docstring
        ancestor_fingerprint = _fingerprint_of(ancestor)
        if ancestor_fingerprint is None:
            return None  # can't compare -- absence is unknown, never a guess
        distance = hamming_distance(fingerprint, ancestor_fingerprint)
        return (ancestor, distance) if distance <= threshold else None
    return None
