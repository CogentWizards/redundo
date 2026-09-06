"""Generic aggregation primitives shared by every analysis -- not specific
to waste detection. `Slice` is a plain (count, cost, tokens) accumulator
over events; `CoverageStats`/`compute_generic_coverage()` measure how much
of the loaded corpus an analysis can actually speak to, before any
analysis-specific headline number.

Anything that isn't generic across *every possible* analysis (candidate
pairs, verdicts, "which tasks had a repeat to compare") doesn't belong
here -- see `analyses/waste.py`, which computes its own analysis-specific
coverage notes and appends them to `CoverageStats.extra_notes` rather than
this module growing a field for every future analysis's own vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import Event


@dataclass
class Slice:
    """Aggregate counters for one (bucket, segment) cell."""

    count: int = 0
    cost_usd: float = 0.0
    unpriced_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, event: Event) -> None:
        self.count += 1
        if event.cost_usd is not None:
            self.cost_usd += event.cost_usd
        else:
            self.unpriced_count += 1
        self.tokens_in += event.tokens_in or 0
        self.tokens_out += event.tokens_out or 0


@dataclass
class CoverageStats:
    """How much of the loaded corpus this analysis can actually speak to.

    Computed over EVERY loaded event, not just the ones a specific
    analysis found interesting -- the point is to tell a reader what
    fraction of their total data the percentages below are even computed
    on, before they trust or forward those percentages. Two dimensions
    are generic enough to live here:

    - pricing: does an event have cost_usd, or only token counts (or
      nothing)? Dollar totals are silently a sum over the priced subset
      only; this is what makes that explicit.
    - task_id confidence: metadata.task_id_source is an adapter-side
      convention (redundo adapt's OpenInference source sets it; other
      sources may not). "trace_id_fallback" means grouping fell back from
      a real conversation id, and that adapter's own docs already say
      what that costs (cross-trace rework not detected). Most sources
      will report neither key, and that's not a defect -- it just means
      this dimension can't be spoken to for that data, so those counters
      stay at 0 rather than guessing.

    `extra_notes` is where an analysis-specific coverage caveat goes
    (e.g. the waste analysis's "N tasks had nothing to compare" note) --
    a free-form list of pre-rendered sentences, not a growing set of
    fields tied to one analysis's own vocabulary.
    """

    total_events: int = 0
    priced_events: int = 0
    unpriced_events: int = 0
    total_priced_cost_usd: float = 0.0

    events_with_task_id_source_reported: int = 0
    events_confident_task_id: int = 0
    events_degraded_task_id: int = 0

    extra_notes: list[str] = field(default_factory=list)

    @property
    def pricing_coverage_fraction(self) -> float:
        return self.priced_events / self.total_events if self.total_events else 0.0

    @property
    def task_id_confidence_fraction(self) -> float | None:
        """Fraction of *events that reported a task_id_source at all* that
        were confidently grouped. None (not 0.0) when no source in this
        corpus reports the field -- that's "can't be spoken to", not "0%".
        """
        if self.events_with_task_id_source_reported == 0:
            return None
        return self.events_confident_task_id / self.events_with_task_id_source_reported


def compute_generic_coverage(events: list[Event]) -> CoverageStats:
    """The two dimensions every analysis can speak to, regardless of what
    that analysis actually looks for. `events` is the full loaded corpus,
    not just the ones a specific analysis found interesting -- coverage is
    measured against everything that was actually loaded, since that's the
    denominator a reader needs to judge how much of their data this
    analysis's percentages are even computed on.
    """
    coverage = CoverageStats(total_events=len(events))

    for event in events:
        if event.cost_usd is not None:
            coverage.priced_events += 1
            coverage.total_priced_cost_usd += event.cost_usd
        else:
            coverage.unpriced_events += 1

        source = event.metadata.get("task_id_source") if isinstance(event.metadata, dict) else None
        if source == "conversation_id":
            coverage.events_with_task_id_source_reported += 1
            coverage.events_confident_task_id += 1
        elif source == "trace_id_fallback":
            coverage.events_with_task_id_source_reported += 1
            coverage.events_degraded_task_id += 1
        # any other value (including absent): not reported by this source,
        # not counted in either direction.

    return coverage
