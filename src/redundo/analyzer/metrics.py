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

from .schema import (
    META_COST_BASIS_KEY,
    META_SOURCE_PATH_KEY,
    META_SYNTHESIZED_COST_ONLY_KEY,
    Event,
)

# What counts as "a call" for the call-volume denominator below: an
# llm_call or a tool_call, the two event types that represent an actual
# invocation. tool_result is the paired outcome of a tool_call, not a
# second call, so it's deliberately excluded here.
_CALL_EVENT_TYPES: frozenset[str] = frozenset({"llm_call", "tool_call"})

# The canonical label used when an event has cost_usd but no
# metadata.cost_basis: the source reported it directly, as-is. No
# adapter ever writes this string; it's what compute_generic_coverage
# falls back to when the key is absent, so "direct" is as visible in a
# report as every estimated/apportioned/synthesized case.
COST_BASIS_DIRECT = "direct_from_source"
# The canonical label for a record with metadata.synthesized_cost_only
# set, overriding whatever (if anything) metadata.cost_basis says: a
# stronger, more specific statement about the record's origin than any
# ordinary cost_basis value.
COST_BASIS_SYNTHESIZED = "synthesized_billing_only"


# Shared convention for "this event had no real model/workflow value" --
# not specific to WasteAnalysis, any analysis segmenting by model or
# workflow should use these same two strings for consistency. A bucket
# built entirely from tool_call events (which never carry a model in
# most sources) or from a source that never sets workflow legitimately
# renders as a single row of this placeholder -- that's the honest
# answer, not a rendering bug, and report.py shows it rather than hiding
# a real (if unlabeled) row of data.
UNKNOWN_MODEL_LABEL = "(unknown model)"
UNLABELED_WORKFLOW_LABEL = "(unlabeled workflow)"


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
class CostBasisStat:
    """Aggregate counters for one cost_basis value (see schema.py's
    META_COST_BASIS_KEY): how many priced events used it, and how many
    dollars they carry. Deliberately just (events, usd) -- unlike
    `Slice`, this isn't sliced by model/workflow, it's a one-dimensional
    breakdown of "how was this dollar figure produced," not "what
    produced it."
    """

    events: int = 0
    usd: float = 0.0


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
    - cost basis: of the events that ARE priced, how many dollars came
      from the source directly versus an adapter's own estimate,
      apportionment, or synthesis (see schema.py's META_COST_BASIS_KEY
      and docs/pricing.md). A single "tracked spend" total mixes these
      silently unless this is reported alongside it; `cost_by_basis`
      is what keeps a real billed dollar and a bundled-price-table
      guess from looking equally authoritative in a report.
    - call volume: total_call_events, the count of llm_call/tool_call
      events in the loaded corpus (never tool_result, the paired half
      of a tool_call, not a second call). Generic enough to live here
      because it isn't specific to any one analysis's own bucket
      shape; report.py uses it to scale a bucket's own dollar figure
      to a hypothetical call volume.
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
    total_call_events: int = 0

    # Keyed by the canonical cost_basis label (COST_BASIS_DIRECT,
    # COST_BASIS_SYNTHESIZED, or an adapter's own metadata.cost_basis
    # string). Every priced event contributes to exactly one entry here;
    # the values sum to priced_events/total_priced_cost_usd above.
    cost_by_basis: dict[str, CostBasisStat] = field(default_factory=dict)

    events_with_task_id_source_reported: int = 0
    events_confident_task_id: int = 0
    events_degraded_task_id: int = 0

    extra_notes: list[str] = field(default_factory=list)
    # A handful of the actual unpriced events, formatted the same way a
    # bucket's own sample reasons are ("task=... step=... (type/name)"),
    # so a reader can go spot-check specific rows instead of just trusting
    # the count. Capped at collection time (see max_samples on
    # compute_generic_coverage), the same reason WasteAnalysis caps its
    # own reasons lists rather than keeping every one for a large corpus.
    unpriced_samples: list[str] = field(default_factory=list)

    # Real spend synthesized from cost-only telemetry with no matching
    # span at all (see schema.py's META_SYNTHESIZED_COST_ONLY_KEY).
    # Already included in priced_events/total_priced_cost_usd above,
    # tracked separately so a reader can see how much of "tracked spend"
    # rests on a real span versus a billing record alone.
    synthesized_cost_only_events: int = 0
    synthesized_cost_only_usd: float = 0.0
    # A handful of the actual synthesized records, same format and same
    # cap as unpriced_samples -- so "there's real untraceable spend" is
    # spot-checkable by hand too, not just a count and a dollar figure.
    synthesized_cost_only_samples: list[str] = field(default_factory=list)

    # The local directory `redundo adapt` read this corpus from (see
    # schema.py's META_SOURCE_PATH_KEY), when every loaded event agrees
    # on the same one. None when no event sets it (hand-built NDJSON, or
    # any path that skips the `redundo adapt` CLI) or when events
    # disagree (a corpus hand-assembled from more than one adapt run) --
    # never a guess at which one to show.
    source_path: str | None = None

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


# Below this denominator, a percentage claims more precision than a
# small sample actually supports -- "50%" on n=2 reads as a real,
# stable rate when it's really one coin flip. format_fraction drops the
# percentage entirely below the threshold and shows the plain fraction
# instead, which is exactly as informative and doesn't overclaim.
_MIN_DENOMINATOR_FOR_PERCENT = 5


def format_fraction(numerator: int, denominator: int, *, min_denominator: int = _MIN_DENOMINATOR_FOR_PERCENT) -> str:
    if denominator <= 0:
        return f"{numerator}/{denominator}"
    frac = f"{numerator}/{denominator}"
    if denominator < min_denominator:
        return frac
    pct = numerator / denominator * 100
    return f"{frac} ({pct:.0f}%)"


def compute_generic_coverage(events: list[Event], *, max_samples: int = 20) -> CoverageStats:
    """The two dimensions every analysis can speak to, regardless of what
    that analysis actually looks for. `events` is the full loaded corpus,
    not just the ones a specific analysis found interesting -- coverage is
    measured against everything that was actually loaded, since that's the
    denominator a reader needs to judge how much of their data this
    analysis's percentages are even computed on.
    """
    coverage = CoverageStats(total_events=len(events))
    source_paths: set[str] = set()

    for event in events:
        if event.event_type in _CALL_EVENT_TYPES:
            coverage.total_call_events += 1

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        synthesized = bool(metadata.get(META_SYNTHESIZED_COST_ONLY_KEY))

        if event.cost_usd is not None:
            coverage.priced_events += 1
            coverage.total_priced_cost_usd += event.cost_usd

            # synthesized_cost_only wins over whatever (if anything)
            # cost_basis says: it's a stronger, more specific statement
            # about where this dollar figure actually came from. Absence
            # of cost_basis is itself the signal for "direct from
            # source," not a gap to skip -- see schema.py's
            # META_COST_BASIS_KEY docstring.
            basis = (
                COST_BASIS_SYNTHESIZED if synthesized
                else metadata.get(META_COST_BASIS_KEY) or COST_BASIS_DIRECT
            )
            stat = coverage.cost_by_basis.setdefault(basis, CostBasisStat())
            stat.events += 1
            stat.usd += event.cost_usd
        else:
            coverage.unpriced_events += 1
            if len(coverage.unpriced_samples) < max_samples:
                coverage.unpriced_samples.append(
                    f"task={event.task_id} step={event.step_index} "
                    f"({event.event_type}/{event.name}): no cost_usd recorded"
                )

        if synthesized:
            coverage.synthesized_cost_only_events += 1
            if event.cost_usd is not None:
                coverage.synthesized_cost_only_usd += event.cost_usd
            if len(coverage.synthesized_cost_only_samples) < max_samples:
                coverage.synthesized_cost_only_samples.append(
                    f"task={event.task_id} step={event.step_index} "
                    f"({event.event_type}/{event.name}): billing-only, no matching span"
                )

        source_path = metadata.get(META_SOURCE_PATH_KEY)
        if isinstance(source_path, str) and source_path:
            source_paths.add(source_path)

        source = metadata.get("task_id_source")
        if source == "conversation_id":
            coverage.events_with_task_id_source_reported += 1
            coverage.events_confident_task_id += 1
        elif source == "trace_id_fallback":
            coverage.events_with_task_id_source_reported += 1
            coverage.events_degraded_task_id += 1
        # any other value (including absent): not reported by this source,
        # not counted in either direction.

    if len(source_paths) == 1:
        coverage.source_path = next(iter(source_paths))

    return coverage
