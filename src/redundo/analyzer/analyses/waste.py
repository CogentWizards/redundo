"""The one analysis this project ships with: classify repeated LLM/tool
calls as confirmed waste, likely legitimate, unclassified, or three
further, lower-confidence buckets: near-duplicate, cross-task
redundancy, and recurring pattern.

Everything candidate-pair-specific (`classify.py`, `cycles.py`) stays
exactly where it was. This module is just the `Analysis` wrapper around
that existing, unmodified pipeline, translating its `Verdict`-keyed
results into the generic `Bucket`/`AnalysisResult` shape `report.py`
actually renders. `RULE_TEXT` lives here, not in `report.py`: it's
analysis-specific prose describing *this* analysis's decision logic, not
renderer data.

None of the three extra buckets are folded into `Verdict`. That enum is
specifically the exact-match, same-task decision output (`classify_pair`
requires an identical `content_hash` *and* a real lineage relationship
within one task to even consider a pair), and each of these is a
different, fuzzier kind of claim:

- `near_duplicate`: a similarity-threshold-based finding, not an exact
  one. See `near_duplicates.py`'s module docstring for how double
  counting with the three exact-match buckets is structurally prevented,
  not just avoided by convention.
- `cross_task_redundancy`: a same-or-similar call across a real,
  source-confirmed task boundary (see `task_graph.py`), never inferred
  from timing or content. Lighter than `classify_pair`'s four-signal
  logic on purpose: write/outcome semantics don't have an unambiguous
  cross-task meaning yet.
- `recurring_pattern`: a same-or-similar call across *unrelated* tasks
  (no confirmed link at all). Explicitly never a waste claim, a
  frequency observation, kept separate so it's never misread as the same
  kind of finding as the other five buckets.

See `cross_task_candidates.py`'s module docstring for how the last two
are found without ever re-litigating a same-task decision `cycles.py`/
`near_duplicates.py` already made.
"""

from __future__ import annotations

from collections import defaultdict

from ..analysis import Analysis, AnalysisResult, Bucket
from ..classify import Verdict, classify_pair
from ..cross_task_candidates import CrossTaskPair, find_cross_task_pairs
from ..cycles import find_candidate_pairs
from ..lineage import group_by_task
from ..metrics import (
    UNKNOWN_MODEL_LABEL,
    UNLABELED_WORKFLOW_LABEL,
    Slice,
    compute_generic_coverage,
)
from ..near_duplicates import DEFAULT_SIMILARITY_THRESHOLD, find_near_duplicate_pairs
from ..schema import Event
from ..task_graph import build_task_graph, same_component

_NEAR_DUPLICATE_KEY = "near_duplicate"
_NEAR_DUPLICATE_LABEL = "Near duplicate"

# The rule itself, printed next to every count rather than left implicit.
# "42 confirmed_waste" is a claim; "42 confirmed_waste -- repeated call,
# unchanged result, no intervening write, task failed" is a claim someone
# can check against one case by hand. That's what makes it credible enough
# to forward. Keep this in sync with classify.py's actual decision logic --
# it's prose describing that logic, not a separate source of truth.
RULE_TEXT: dict[Verdict, str] = {
    Verdict.CONFIRMED_WASTE: (
        "The call repeated, the result didn't change, nothing wrote to state in "
        "between, and the task still failed. All four have to be true. Drop any "
        "one and this is a guess, not a finding."
    ),
    Verdict.LIKELY_LEGITIMATE: (
        "A specific reason it's not waste: the result changed (polling worked), "
        "a write intervened (verification), or the task succeeded and neither "
        "the result nor the write status already confirms waste on its own."
    ),
    Verdict.UNCLASSIFIED: (
        "Everything else. A required signal (the result, the write status, or "
        "the outcome) was missing from the trace, or the call already looks "
        "wasteful on its own and the task's overall success can't settle "
        "whether it actually mattered. No verdict here, and that's on purpose."
    ),
}

# Deliberately not phrased as a verdict ("waste"/"legitimate") -- this
# bucket only claims similarity, at a threshold someone chose (see
# near_duplicates.DEFAULT_SIMILARITY_THRESHOLD), never an outcome.
NEAR_DUPLICATE_RULE_TEXT = (
    "Arguments are similar but not identical to an earlier call on the same "
    "execution path (a SimHash fingerprint comparison, not exact content_hash "
    "equality). Surfaced for manual review, not a waste or legitimate verdict. "
    "See docs/hashing.md for what a similarity fingerprint can and can't "
    "support."
)

_CROSS_TASK_REDUNDANCY_KEY = "cross_task_redundancy"
_CROSS_TASK_REDUNDANCY_LABEL = "Cross-task redundancy"
_RECURRING_PATTERN_KEY = "recurring_pattern"
_RECURRING_PATTERN_LABEL = "Recurring pattern"

# Also not phrased as a verdict: a real, source-confirmed link connects
# the two tasks (see task_graph.py), but whether the repeat actually
# wasted anything isn't checked here, see the module docstring.
CROSS_TASK_REDUNDANCY_RULE_TEXT = (
    "Same or near-identical call as an earlier one in a different task, and the "
    "two tasks are confirmed related (a source-reported delegation link, never "
    "inferred from timing or content). Surfaced for review, not a waste "
    "verdict: what changed between the two calls isn't checked here yet."
)
# Deliberately not phrased as a finding about the two tasks at all: no
# link connects them, so this is purely a statement about how often this
# content recurs, never a claim that it's related, wasteful, or legitimate.
RECURRING_PATTERN_RULE_TEXT = (
    "Same or near-identical call recurring across tasks with no confirmed "
    "relationship to each other. Not a waste or legitimate verdict, and not "
    "evidence the two tasks are related, most likely a common or generic "
    "operation, not redundant work."
)

# One short, prescriptive line per bucket -- optional on Bucket itself (see
# analysis.py), populated here because only this analysis knows what its
# own buckets mean well enough to recommend anything.
ACTION_TEXT: dict[Verdict, str] = {
    Verdict.CONFIRMED_WASTE: "Cache the result or guard the retry. This spend bought nothing.",
    Verdict.LIKELY_LEGITIMATE: "Leave these alone. Cache them and you'll break polling and verification.",
    Verdict.UNCLASSIFIED: "Emit result hashes and task outcome, then re-run to get a verdict.",
}

# A framing line, not an evidence rule and not a prescription -- what a
# confirmed_waste pair actually means for the agent that made it. Only
# set for this one bucket: it's a specific reading of a specific
# verdict, not a generic sentence report.py could apply anywhere.
CONFIRMED_WASTE_INSIGHT_TEXT = (
    "Your agent repeated itself, learned nothing new, and still failed: "
    "these are the places it was stuck, not working."
)

# How many ranked pairs "Fix these first" surfaces. Small on purpose --
# this is meant to read as a short, actionable list, not a second copy
# of the sample-cases section below it.
_TOP_HIGHLIGHTS = 3
NEAR_DUPLICATE_ACTION_TEXT = "Nothing to do. When these appear, read them by hand."
CROSS_TASK_REDUNDANCY_ACTION_TEXT = (
    "Read these by hand. A confirmed link exists between the two tasks, but not "
    "yet enough signal here to call it waste or legitimate."
)
RECURRING_PATTERN_ACTION_TEXT = (
    "Nothing to do by default. If this recurs a lot, it may be worth caching or "
    "memoizing globally, but it isn't evidence of wasted spend on its own."
)

_ORDER = (Verdict.CONFIRMED_WASTE, Verdict.LIKELY_LEGITIMATE, Verdict.UNCLASSIFIED)
_LABELS = {
    Verdict.CONFIRMED_WASTE: "Confirmed waste",
    Verdict.LIKELY_LEGITIMATE: "Likely legitimate",
    Verdict.UNCLASSIFIED: "Unclassified",
}


class WasteAnalysis(Analysis):
    name = "waste"

    def __init__(
        self,
        *,
        keep_reasons: int = 20,
        near_duplicate_threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._keep_reasons = keep_reasons
        self._near_duplicate_threshold = near_duplicate_threshold

    def run(self, events: list[Event]) -> AnalysisResult:
        lineages = group_by_task(events)
        pairs = find_candidate_pairs(events, lineages=lineages)
        classifications = [classify_pair(pair, lineages[pair.task_id]) for pair in pairs]
        near_pairs = find_near_duplicate_pairs(
            events,
            lineages=lineages,
            exact_pairs=pairs,
            threshold=self._near_duplicate_threshold,
        )

        task_graph = build_task_graph(events)
        cross_task_pairs = find_cross_task_pairs(
            events, exact_pairs=pairs, near_pairs=near_pairs,
            threshold=self._near_duplicate_threshold,
        )
        cross_task_redundancy_pairs = [
            p for p in cross_task_pairs
            if same_component(task_graph, p.original.task_id, p.repeat.task_id)
        ]
        recurring_pattern_pairs = [
            p for p in cross_task_pairs
            if not same_component(task_graph, p.original.task_id, p.repeat.task_id)
        ]

        coverage = compute_generic_coverage(events)
        self._add_comparability_note(coverage, events, classifications)
        self._add_near_duplicate_comparability_note(coverage, events, near_pairs)
        self._add_cross_task_notes(coverage, cross_task_redundancy_pairs, recurring_pattern_pairs)

        slices = {v: Slice() for v in _ORDER}
        by_model = {v: defaultdict(Slice) for v in _ORDER}
        by_workflow = {v: defaultdict(Slice) for v in _ORDER}
        reasons: dict[Verdict, list[str]] = {v: [] for v in _ORDER}

        for c in classifications:
            repeat = c.pair.repeat
            slices[c.verdict].add(repeat)
            by_model[c.verdict][repeat.model or UNKNOWN_MODEL_LABEL].add(repeat)
            by_workflow[c.verdict][repeat.workflow or UNLABELED_WORKFLOW_LABEL].add(repeat)
            if len(reasons[c.verdict]) < self._keep_reasons:
                reasons[c.verdict].append(
                    f"task={c.pair.task_id} step={repeat.step_index} "
                    f"({repeat.event_type}/{repeat.name}): {c.reason}"
                )

        highlights = self._highlights(classifications)

        near_dup_slice, near_dup_by_model, near_dup_by_workflow, near_dup_reasons = (
            self._similarity_bucket_data(
                near_pairs, self._keep_reasons,
                lambda pair: (
                    f"task={pair.task_id} step={pair.repeat.step_index} "
                    f"({pair.repeat.event_type}/{pair.repeat.name}): similar to step="
                    f"{pair.original.step_index} (Hamming distance "
                    f"{pair.hamming_distance}/64 bits, threshold "
                    f"{self._near_duplicate_threshold}): similar, not identical; "
                    "not a waste verdict"
                ),
            )
        )
        cross_task_slice, cross_task_by_model, cross_task_by_workflow, cross_task_reasons = (
            self._similarity_bucket_data(
                cross_task_redundancy_pairs, self._keep_reasons, self._cross_task_reason,
            )
        )
        recurring_slice, recurring_by_model, recurring_by_workflow, recurring_reasons = (
            self._similarity_bucket_data(
                recurring_pattern_pairs, self._keep_reasons, self._cross_task_reason,
            )
        )

        buckets = [
            Bucket(
                key=v.value, label=_LABELS[v], rule_text=RULE_TEXT[v], slice=slices[v],
                action_text=ACTION_TEXT[v],
                insight_text=(
                    CONFIRMED_WASTE_INSIGHT_TEXT if v is Verdict.CONFIRMED_WASTE else None
                ),
            )
            for v in _ORDER
        ]
        buckets.append(
            Bucket(
                key=_NEAR_DUPLICATE_KEY,
                label=_NEAR_DUPLICATE_LABEL,
                rule_text=NEAR_DUPLICATE_RULE_TEXT,
                slice=near_dup_slice,
                action_text=NEAR_DUPLICATE_ACTION_TEXT,
            )
        )
        buckets.append(
            Bucket(
                key=_CROSS_TASK_REDUNDANCY_KEY,
                label=_CROSS_TASK_REDUNDANCY_LABEL,
                rule_text=CROSS_TASK_REDUNDANCY_RULE_TEXT,
                slice=cross_task_slice,
                action_text=CROSS_TASK_REDUNDANCY_ACTION_TEXT,
            )
        )
        buckets.append(
            Bucket(
                key=_RECURRING_PATTERN_KEY,
                label=_RECURRING_PATTERN_LABEL,
                rule_text=RECURRING_PATTERN_RULE_TEXT,
                slice=recurring_slice,
                action_text=RECURRING_PATTERN_ACTION_TEXT,
            )
        )

        waste = slices[Verdict.CONFIRMED_WASTE].count
        legit = slices[Verdict.LIKELY_LEGITIMATE].count
        unclassified = slices[Verdict.UNCLASSIFIED].count
        if unclassified > waste + legit:
            footnote = (
                "Most candidate pairs are unclassified. That means the trace is missing "
                "signal (write flags, result correlation, or terminal outcome), not that "
                "this tool is being conservative for its own sake. A large unclassified "
                "bucket is the honest answer, not a defect. See the project README."
            )
        else:
            footnote = (
                "Unclassified pairs are reported with a count and no verdict, deliberately: "
                "a confident wrong classification here is worse than an honest unknown."
            )

        # How often a repeat actually got a real verdict (confirmed_waste
        # or likely_legitimate) rather than unclassified, over the three
        # real Verdict buckets only -- near_duplicate/cross_task_redundancy/
        # recurring_pattern are a different kind of finding (similarity,
        # never a verdict), so they don't belong in this denominator.
        # This is deliberately NOT a dollar figure: it's a claim about
        # how often the trace carried enough signal to decide at all,
        # which is what actually distinguishes an adjudicator from a
        # tracing UI, not how much of the corpus happened to carry a
        # price. None when there's nothing to compute it from.
        verdicted = waste + legit
        verdictable = verdicted + unclassified
        confidence_stat = None
        if verdictable > 0:
            pct = verdicted / verdictable * 100
            confidence_stat = (
                "Verdicts reached",
                f"{pct:.0f}%",
                f"{verdicted} of {verdictable} exact repeats got a real verdict "
                f"· {unclassified} unclassified for missing signal",
            )

        by_bucket_and_model = {v.value: dict(by_model[v]) for v in _ORDER}
        by_bucket_and_model[_NEAR_DUPLICATE_KEY] = dict(near_dup_by_model)
        by_bucket_and_model[_CROSS_TASK_REDUNDANCY_KEY] = dict(cross_task_by_model)
        by_bucket_and_model[_RECURRING_PATTERN_KEY] = dict(recurring_by_model)
        by_bucket_and_workflow = {v.value: dict(by_workflow[v]) for v in _ORDER}
        by_bucket_and_workflow[_NEAR_DUPLICATE_KEY] = dict(near_dup_by_workflow)
        by_bucket_and_workflow[_CROSS_TASK_REDUNDANCY_KEY] = dict(cross_task_by_workflow)
        by_bucket_and_workflow[_RECURRING_PATTERN_KEY] = dict(recurring_by_workflow)
        all_reasons = {v.value: reasons[v] for v in _ORDER}
        all_reasons[_NEAR_DUPLICATE_KEY] = near_dup_reasons
        all_reasons[_CROSS_TASK_REDUNDANCY_KEY] = cross_task_reasons
        all_reasons[_RECURRING_PATTERN_KEY] = recurring_reasons

        return AnalysisResult(
            coverage=coverage,
            buckets=buckets,
            by_bucket_and_model=by_bucket_and_model,
            by_bucket_and_workflow=by_bucket_and_workflow,
            reasons=all_reasons,
            total_candidates=(
                len(classifications) + len(near_pairs) + len(cross_task_pairs)
            ),
            analysis_name=self.name,
            footnote=footnote,
            highlights=highlights,
            confidence_stat=confidence_stat,
        )

    @staticmethod
    def _highlights(classifications) -> list[str]:
        """The top `_TOP_HIGHLIGHTS` confirmed_waste pairs, ranked by their
        own real cost_usd, descending. Never ranks by a projection or any
        other derived figure -- only a real, directly-reported dollar
        amount is a defensible ranking key. Returns [] when no
        confirmed_waste pair has cost data to rank by, rather than
        falling back to an arbitrary order dressed up as a ranking.
        """
        priced = [
            c for c in classifications
            if c.verdict is Verdict.CONFIRMED_WASTE and c.pair.repeat.cost_usd is not None
        ]
        priced.sort(key=lambda c: c.pair.repeat.cost_usd, reverse=True)

        highlights = []
        for rank, c in enumerate(priced[:_TOP_HIGHLIGHTS], start=1):
            repeat = c.pair.repeat
            cost_text = f"${repeat.cost_usd:,.4f}" if repeat.cost_usd < 1 else f"${repeat.cost_usd:,.2f}"
            highlights.append(
                f"{rank}. {cost_text} for task={c.pair.task_id} step={repeat.step_index} "
                f"({repeat.event_type}/{repeat.name}): {c.reason}"
            )
        return highlights

    @staticmethod
    def _add_comparability_note(coverage, events: list[Event], classifications) -> None:
        """Comparability isn't a data gap -- every call in a task with zero
        candidate pairs was simply unique, so redundancy detection had
        nothing to compare. But it also never appears in any bucket above,
        since those are built from classified pairs, not tasks. Without
        this note, "this task's spend had nothing repeated" and "this
        task's spend belongs to a source with missing signal" would both
        just be silence in the bucket breakdown, indistinguishable from
        each other.
        """
        task_ids_with_pairs = {c.pair.task_id for c in classifications}
        all_task_ids = {e.task_id for e in events}
        tasks_total = len(all_task_ids)
        tasks_with_pairs = len(task_ids_with_pairs)

        events_without = 0
        cost_without = 0.0
        for e in events:
            if e.task_id not in task_ids_with_pairs:
                events_without += 1
                if e.cost_usd is not None:
                    cost_without += e.cost_usd

        if not events_without:
            return
        pct = (tasks_with_pairs / tasks_total * 100) if tasks_total else 0.0
        cost_text = f"${cost_without:,.4f}" if cost_without < 1 else f"${cost_without:,.2f}"
        coverage.extra_notes.append(
            f"{tasks_with_pairs}/{tasks_total} tasks ({pct:.0f}%) had at least one "
            "repeated call for redundancy detection to examine. The rest, "
            f"{cost_text} of tracked spend across {events_without} event(s), had "
            "nothing that repeated at all, so nothing appears for them in the "
            "buckets below. That's not a gap in the data; every call in those "
            "tasks was simply unique."
        )

    @staticmethod
    def _add_near_duplicate_comparability_note(coverage, events: list[Event], near_pairs) -> None:
        """A separate note from _add_comparability_note above, by design --
        near-duplicate is a lower-confidence, threshold-based signal, and
        conflating its coverage with the exact-match note would blur two
        claims of different strength into one number. Silent (no note at
        all) when zero near-duplicate pairs were found -- the common case
        for most sessions -- rather than adding a line of noise to every
        report for a feature that found nothing this time.
        """
        if not near_pairs:
            return
        task_ids_with_near_pairs = {p.task_id for p in near_pairs}
        tasks_total = len({e.task_id for e in events})
        tasks_with_near_pairs = len(task_ids_with_near_pairs)
        pct = (tasks_with_near_pairs / tasks_total * 100) if tasks_total else 0.0
        coverage.extra_notes.append(
            f"{tasks_with_near_pairs}/{tasks_total} tasks ({pct:.0f}%) also had at least "
            f"one near-duplicate call ({len(near_pairs)} such pair(s) total): similar, "
            "not identical, arguments to an earlier call on the same execution path. A "
            "separate, lower-confidence signal from the exact-match note above; see the "
            "near_duplicate bucket below."
        )

    @staticmethod
    def _add_cross_task_notes(coverage, cross_task_redundancy_pairs, recurring_pattern_pairs) -> None:
        """Two more separate notes, same reasoning as
        _add_near_duplicate_comparability_note above: each is its own
        distinct claim, at its own confidence level, and conflating either
        with the others would blur what's actually being reported. Silent
        for whichever finding had zero pairs.
        """
        if cross_task_redundancy_pairs:
            coverage.extra_notes.append(
                f"{len(cross_task_redundancy_pairs)} pair(s) found across tasks confirmed "
                "to be part of the same real workflow (a source-reported delegation link, "
                "never inferred from timing or content); see the cross_task_redundancy "
                "bucket below."
            )
        if recurring_pattern_pairs:
            coverage.extra_notes.append(
                f"{len(recurring_pattern_pairs)} pair(s) found across tasks with no "
                "confirmed relationship to each other. Not a waste claim, see the "
                "recurring_pattern bucket below."
            )

    @staticmethod
    def _similarity_bucket_data(pairs, keep_reasons: int, reason_fn):
        """Shared Slice/by_model/by_workflow/reasons assembly for any bucket
        built from a flat list of (original, repeat, ...) pairs rather than
        classify.py Classifications. near_duplicate,
        cross_task_redundancy, and recurring_pattern all have this same
        shape.
        """
        slice_ = Slice()
        by_model: dict[str, Slice] = defaultdict(Slice)
        by_workflow: dict[str, Slice] = defaultdict(Slice)
        reasons: list[str] = []
        for pair in pairs:
            repeat = pair.repeat
            slice_.add(repeat)
            by_model[repeat.model or UNKNOWN_MODEL_LABEL].add(repeat)
            by_workflow[repeat.workflow or UNLABELED_WORKFLOW_LABEL].add(repeat)
            if len(reasons) < keep_reasons:
                reasons.append(reason_fn(pair))
        return slice_, by_model, by_workflow, reasons

    def _cross_task_reason(self, pair: CrossTaskPair) -> str:
        match_desc = (
            "exact match" if pair.hamming_distance == 0
            else f"similar (Hamming distance {pair.hamming_distance}/64 bits, "
                 f"threshold {self._near_duplicate_threshold})"
        )
        return (
            f"task={pair.repeat.task_id} step={pair.repeat.step_index} "
            f"({pair.repeat.event_type}/{pair.repeat.name}): {match_desc} to "
            f"task={pair.original.task_id} step={pair.original.step_index}"
        )
