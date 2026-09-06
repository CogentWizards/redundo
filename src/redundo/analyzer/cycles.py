"""Find candidate redundant-repeat pairs.

A candidate pair is (original, repeat): two events in the same task, same
event_type, same name, same content_hash -- identical arguments -- where
`original` is the nearest matching ancestor of `repeat` along its lineage
(see lineage.py for what "ancestor" means here).

"Nearest" is a deliberate choice, not just a way to avoid double-counting.
The lookup walks the *entire* ancestor chain to the task root checking
every ancestor's signature, not merely the immediate parent -- it just
stops at the first (closest) match rather than collecting every one.
Farther matches up the same chain are real and expected in a 3+ call
identical chain, and are intentionally left unpaired: `intervening`
(events between original and repeat) is what classify.py reads to ask "did
anything change since the last time this exact call happened," and that
question is only answered correctly by the *most recent* occurrence. Given
A -> X(write) -> B -> C all sharing a signature, pairing (A, C) directly
would report intervening=[X, B], and the write in X would make C look
legitimate even though nothing changed between B and C specifically.
Pairing nearest-first instead gives (A, B) [intervening=X, legit] and
(B, C) [intervening=(), a real candidate for waste] -- each pair asks
about the actual gap that matters, not a gap that happens to contain an
unrelated earlier write.

Finding a candidate pair says nothing about whether it's waste yet. That's
classify.py's job; this module only answers "was this call redundant with
an earlier one in the same execution path."
"""

from __future__ import annotations

from dataclasses import dataclass

from .lineage import TaskLineage, group_by_task
from .schema import Event


@dataclass(frozen=True, slots=True)
class CandidatePair:
    task_id: str
    original: Event
    repeat: Event
    intervening: tuple[Event, ...]  # events strictly between original and repeat, in order

    @property
    def signature(self) -> tuple[str, str, str]:
        return (self.repeat.event_type, self.repeat.name, self.repeat.content_hash)


def find_candidate_pairs(
    events: list[Event],
    *,
    lineages: dict[str, TaskLineage] | None = None,
) -> list[CandidatePair]:
    """One pair per event that has a matching ancestor -- the nearest one,
    not every one. A 3-call identical chain A -> B -> C yields (A, B) and
    (B, C), not (A, B), (A, C), (B, C): each event is only ever "the
    repeat" once, so cost attribution (metrics.py sums the repeat side of
    each pair) doesn't double-count.

    `lineages`: pass an already-computed `group_by_task(events)` result if
    the caller needs it for anything else too (e.g. per-pair classification
    against the same lineage) -- avoids computing it twice. Computed fresh
    when omitted, so existing callers need no changes.
    """
    if lineages is None:
        lineages = group_by_task(events)
    pairs: list[CandidatePair] = []
    for lineage in lineages.values():
        pairs.extend(_pairs_for_task(lineage))
    return pairs


def _pairs_for_task(lineage: TaskLineage) -> list[CandidatePair]:
    pairs: list[CandidatePair] = []
    for event in lineage.events:
        if event.event_type not in ("llm_call", "tool_call"):
            # tool_result rows are outcomes, not invocations -- they're
            # correlated to their call in classify.py, not treated as
            # repeatable actions themselves.
            continue
        match = _nearest_matching_ancestor(lineage, event)
        if match is None:
            continue
        intervening = tuple(reversed(lineage.path_between(match, event)))
        pairs.append(
            CandidatePair(
                task_id=lineage.task_id,
                original=match,
                repeat=event,
                intervening=intervening,
            )
        )
    return pairs


def _nearest_matching_ancestor(lineage: TaskLineage, event: Event) -> Event | None:
    # ancestors_of() yields nearest-first up to the task root, so returning
    # on the first hit here already scans the full chain -- it just prefers
    # the closest match over a farther one, for the reasons in the module
    # docstring (the "did anything change since X" question above needs the
    # most recent occurrence, not the first one).
    signature = (event.event_type, event.name, event.content_hash)
    for ancestor in lineage.ancestors_of(event):
        if (ancestor.event_type, ancestor.name, ancestor.content_hash) == signature:
            return ancestor
    return None
