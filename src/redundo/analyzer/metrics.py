"""Aggregate classifications into counts and cost.

"Waste" is attributed to the repeat side of each pair, not the original --
the first call in a chain is presumably necessary; only the re-calls are
candidate waste. A 3-call identical chain (A -> B -> C) is two candidate
pairs, (A,B) and (B,C), so B and C each get counted once as "the repeat"
and the chain contributes two units of waste, not three or one.

cost_usd is used directly when present -- no price table is maintained by
this package (the caller wasn't asking for one, and hardcoded prices go
stale). When cost_usd is absent, dollar totals for that slice are left at
0 and reported number of "unpriced" repeats is surfaced instead, alongside
token totals and a per-model breakdown, so a price can be applied outside
this package if wanted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .classify import Classification, Verdict
from .schema import Event


@dataclass
class Slice:
    """Aggregate counters for one (bucket, segment) cell."""

    count: int = 0
    cost_usd: float = 0.0
    unpriced_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, classification: Classification) -> None:
        repeat = classification.pair.repeat
        self.count += 1
        if repeat.cost_usd is not None:
            self.cost_usd += repeat.cost_usd
        else:
            self.unpriced_count += 1
        self.tokens_in += repeat.tokens_in or 0
        self.tokens_out += repeat.tokens_out or 0


@dataclass
class CoverageStats:
    """How much of the loaded corpus this analysis can actually speak to.

    Computed over EVERY loaded event, not just the ones that ended up in a
    candidate pair -- the point is to tell a reader what fraction of their
    total data the percentages below are even computed on, before they
    trust or forward those percentages. Three dimensions:

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
    - comparability: a task with zero candidate pairs isn't a data gap --
      every call in it was simply unique, so redundancy detection had
      nothing to compare. But it also never appears in any of the three
      verdict buckets, since those are built from classified pairs, not
      tasks. Without this, "this task's spend had nothing repeated" and
      "this task's spend belongs to a source with missing signal" are
      both just silence in the bucket breakdown, indistinguishable from
      each other. This dimension makes that distinction explicit instead
      of leaving a reader to assume "unclassified" means the same thing
      as "not present in the report at all" -- it doesn't.
    """

    total_events: int = 0
    priced_events: int = 0
    unpriced_events: int = 0
    total_priced_cost_usd: float = 0.0

    events_with_task_id_source_reported: int = 0
    events_confident_task_id: int = 0
    events_degraded_task_id: int = 0

    tasks_total: int = 0
    tasks_with_candidate_pairs: int = 0
    events_in_tasks_with_no_candidate_pairs: int = 0
    cost_usd_in_tasks_with_no_candidate_pairs: float = 0.0

    @property
    def pricing_coverage_fraction(self) -> float:
        return self.priced_events / self.total_events if self.total_events else 0.0

    @property
    def tasks_with_candidate_pairs_fraction(self) -> float:
        return self.tasks_with_candidate_pairs / self.tasks_total if self.tasks_total else 0.0

    @property
    def task_id_confidence_fraction(self) -> float | None:
        """Fraction of *events that reported a task_id_source at all* that
        were confidently grouped. None (not 0.0) when no source in this
        corpus reports the field -- that's "can't be spoken to", not "0%".
        """
        if self.events_with_task_id_source_reported == 0:
            return None
        return self.events_confident_task_id / self.events_with_task_id_source_reported


@dataclass
class Report:
    total_candidate_pairs: int = 0
    coverage: CoverageStats = field(default_factory=CoverageStats)
    by_verdict: dict[Verdict, Slice] = field(
        default_factory=lambda: {v: Slice() for v in Verdict}
    )
    by_verdict_and_model: dict[Verdict, dict[str, Slice]] = field(
        default_factory=lambda: {v: defaultdict(Slice) for v in Verdict}
    )
    by_verdict_and_workflow: dict[Verdict, dict[str, Slice]] = field(
        default_factory=lambda: {v: defaultdict(Slice) for v in Verdict}
    )
    reasons: dict[Verdict, list[str]] = field(default_factory=lambda: {v: [] for v in Verdict})

    def as_dict(self) -> dict:
        def slice_dict(s: Slice) -> dict:
            return {
                "count": s.count,
                "cost_usd": round(s.cost_usd, 6),
                "unpriced_count": s.unpriced_count,
                "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out,
            }

        return {
            "total_candidate_pairs": self.total_candidate_pairs,
            "coverage": {
                "total_events": self.coverage.total_events,
                "priced_events": self.coverage.priced_events,
                "unpriced_events": self.coverage.unpriced_events,
                "total_priced_cost_usd": round(self.coverage.total_priced_cost_usd, 6),
                "pricing_coverage_fraction": round(self.coverage.pricing_coverage_fraction, 4),
                "events_with_task_id_source_reported": (
                    self.coverage.events_with_task_id_source_reported
                ),
                "events_confident_task_id": self.coverage.events_confident_task_id,
                "events_degraded_task_id": self.coverage.events_degraded_task_id,
                "task_id_confidence_fraction": self.coverage.task_id_confidence_fraction,
                "tasks_total": self.coverage.tasks_total,
                "tasks_with_candidate_pairs": self.coverage.tasks_with_candidate_pairs,
                "tasks_with_candidate_pairs_fraction": round(
                    self.coverage.tasks_with_candidate_pairs_fraction, 4
                ),
                "events_in_tasks_with_no_candidate_pairs": (
                    self.coverage.events_in_tasks_with_no_candidate_pairs
                ),
                "cost_usd_in_tasks_with_no_candidate_pairs": round(
                    self.coverage.cost_usd_in_tasks_with_no_candidate_pairs, 6
                ),
            },
            "by_verdict": {v.value: slice_dict(s) for v, s in self.by_verdict.items()},
            "by_verdict_and_model": {
                v.value: {model: slice_dict(s) for model, s in models.items()}
                for v, models in self.by_verdict_and_model.items()
            },
            "by_verdict_and_workflow": {
                v.value: {wf: slice_dict(s) for wf, s in wfs.items()}
                for v, wfs in self.by_verdict_and_workflow.items()
            },
        }


def _compute_coverage(events: list[Event], classifications: list[Classification]) -> CoverageStats:
    coverage = CoverageStats(total_events=len(events))

    # Tasks with >=1 candidate pair had something to compare, whatever the
    # pairs classified as. Tasks absent from this set aren't a data gap --
    # nothing in them repeated, so redundancy detection had nothing to do.
    task_ids_with_pairs = {c.pair.task_id for c in classifications}
    all_task_ids = {event.task_id for event in events}
    coverage.tasks_total = len(all_task_ids)
    coverage.tasks_with_candidate_pairs = len(task_ids_with_pairs)

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

        if event.task_id not in task_ids_with_pairs:
            coverage.events_in_tasks_with_no_candidate_pairs += 1
            if event.cost_usd is not None:
                coverage.cost_usd_in_tasks_with_no_candidate_pairs += event.cost_usd
    return coverage


def build_report(
    classifications: list[Classification],
    events: list[Event],
    *,
    keep_reasons: int = 20,
) -> Report:
    """`events` is the full loaded corpus (not just the events that appear
    in `classifications`) -- coverage is measured against everything that
    was actually loaded, since that's the denominator a reader needs to
    judge how much of their data this report can speak to.
    """
    report = Report()
    report.total_candidate_pairs = len(classifications)
    report.coverage = _compute_coverage(events, classifications)

    for classification in classifications:
        verdict = classification.verdict
        repeat = classification.pair.repeat

        report.by_verdict[verdict].add(classification)

        model_key = repeat.model or "(unknown model)"
        report.by_verdict_and_model[verdict][model_key].add(classification)

        workflow_key = repeat.workflow or "(unlabeled workflow)"
        report.by_verdict_and_workflow[verdict][workflow_key].add(classification)

        if len(report.reasons[verdict]) < keep_reasons:
            report.reasons[verdict].append(
                f"task={classification.pair.task_id} "
                f"step={repeat.step_index} ({repeat.event_type}/{repeat.name}): "
                f"{classification.reason}"
            )

    return report
