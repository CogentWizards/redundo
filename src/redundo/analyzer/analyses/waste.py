"""The one analysis this project ships with: classify repeated LLM/tool
calls as confirmed waste, likely legitimate, unclassified, or -- a fourth,
lower-confidence bucket -- near-duplicate.

Everything candidate-pair-specific (`classify.py`, `cycles.py`) stays
exactly where it was -- this module is just the `Analysis` wrapper around
that existing, unmodified pipeline, translating its `Verdict`-keyed
results into the generic `Bucket`/`AnalysisResult` shape `report.py`
actually renders. `RULE_TEXT` lives here, not in `report.py`: it's
analysis-specific prose describing *this* analysis's decision logic, not
renderer data.

The near-duplicate bucket is deliberately NOT folded into `Verdict` --
that enum is specifically the exact-match decision output (`classify_pair`
requires an identical `content_hash` to even consider a pair), and a
similarity-threshold-based finding is a different, fuzzier kind of claim.
Giving it its own bucket with its own rule text keeps that distinction
visible in the report rather than overstating confidence in something a
SimHash fingerprint comparison alone can't support. See
`near_duplicates.py`'s module docstring for how double-counting with the
three exact-match buckets is structurally prevented, not just avoided by
convention.
"""

from __future__ import annotations

from collections import defaultdict

from ..analysis import Analysis, AnalysisResult, Bucket
from ..classify import Verdict, classify_pair
from ..cycles import find_candidate_pairs
from ..lineage import group_by_task
from ..metrics import Slice, compute_generic_coverage
from ..near_duplicates import DEFAULT_SIMILARITY_THRESHOLD, find_near_duplicate_pairs
from ..schema import Event

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
        "repeated call, unchanged result, no intervening write, task failed. "
        "All four, confirmed -- drop any one and it's a guess, not a finding."
    ),
    Verdict.LIKELY_LEGITIMATE: (
        "a specific reason it's not waste: result changed (polling worked), "
        "a write intervened (verification), or the task succeeded and neither "
        "the result nor the write status is already confirmed waste on its own"
    ),
    Verdict.UNCLASSIFIED: (
        "everything else -- a required signal (result, write status, or "
        "outcome) was missing from the trace, or the call confirms waste on "
        "its own and task-level success can't settle whether it mattered. "
        "No verdict, on purpose"
    ),
}

# Deliberately not phrased as a verdict ("waste"/"legitimate") -- this
# bucket only claims similarity, at a threshold someone chose (see
# near_duplicates.DEFAULT_SIMILARITY_THRESHOLD), never an outcome.
NEAR_DUPLICATE_RULE_TEXT = (
    "arguments are similar but not identical to an earlier call on the same "
    "execution path (SimHash fingerprint comparison, not exact content_hash "
    "equality) -- surfaced for manual review, not a waste/legitimate verdict. "
    "See docs/hashing.md for what a similarity fingerprint can and can't "
    "support."
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

        coverage = compute_generic_coverage(events)
        self._add_comparability_note(coverage, events, classifications)
        self._add_near_duplicate_comparability_note(coverage, events, near_pairs)

        slices = {v: Slice() for v in _ORDER}
        by_model = {v: defaultdict(Slice) for v in _ORDER}
        by_workflow = {v: defaultdict(Slice) for v in _ORDER}
        reasons: dict[Verdict, list[str]] = {v: [] for v in _ORDER}

        for c in classifications:
            repeat = c.pair.repeat
            slices[c.verdict].add(repeat)
            by_model[c.verdict][repeat.model or "(unknown model)"].add(repeat)
            by_workflow[c.verdict][repeat.workflow or "(unlabeled workflow)"].add(repeat)
            if len(reasons[c.verdict]) < self._keep_reasons:
                reasons[c.verdict].append(
                    f"task={c.pair.task_id} step={repeat.step_index} "
                    f"({repeat.event_type}/{repeat.name}): {c.reason}"
                )

        near_dup_slice = Slice()
        near_dup_by_model: dict[str, Slice] = defaultdict(Slice)
        near_dup_by_workflow: dict[str, Slice] = defaultdict(Slice)
        near_dup_reasons: list[str] = []
        for pair in near_pairs:
            repeat = pair.repeat
            near_dup_slice.add(repeat)
            near_dup_by_model[repeat.model or "(unknown model)"].add(repeat)
            near_dup_by_workflow[repeat.workflow or "(unlabeled workflow)"].add(repeat)
            if len(near_dup_reasons) < self._keep_reasons:
                near_dup_reasons.append(
                    f"task={pair.task_id} step={repeat.step_index} "
                    f"({repeat.event_type}/{repeat.name}): similar to step="
                    f"{pair.original.step_index} (Hamming distance "
                    f"{pair.hamming_distance}/64 bits, threshold "
                    f"{self._near_duplicate_threshold}) -- similar, not identical; "
                    "not a waste verdict"
                )

        buckets = [
            Bucket(key=v.value, label=_LABELS[v], rule_text=RULE_TEXT[v], slice=slices[v])
            for v in _ORDER
        ]
        buckets.append(
            Bucket(
                key=_NEAR_DUPLICATE_KEY,
                label=_NEAR_DUPLICATE_LABEL,
                rule_text=NEAR_DUPLICATE_RULE_TEXT,
                slice=near_dup_slice,
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
                "bucket is the honest answer, not a defect -- see the project README."
            )
        else:
            footnote = (
                "Unclassified pairs are reported with a count and no verdict, deliberately: "
                "a confident wrong classification here is worse than an honest unknown."
            )

        by_bucket_and_model = {v.value: dict(by_model[v]) for v in _ORDER}
        by_bucket_and_model[_NEAR_DUPLICATE_KEY] = dict(near_dup_by_model)
        by_bucket_and_workflow = {v.value: dict(by_workflow[v]) for v in _ORDER}
        by_bucket_and_workflow[_NEAR_DUPLICATE_KEY] = dict(near_dup_by_workflow)
        all_reasons = {v.value: reasons[v] for v in _ORDER}
        all_reasons[_NEAR_DUPLICATE_KEY] = near_dup_reasons

        return AnalysisResult(
            coverage=coverage,
            buckets=buckets,
            by_bucket_and_model=by_bucket_and_model,
            by_bucket_and_workflow=by_bucket_and_workflow,
            reasons=all_reasons,
            total_candidates=len(classifications) + len(near_pairs),
            analysis_name=self.name,
            footnote=footnote,
        )

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
            "repeated call for redundancy detection to examine. The rest -- "
            f"{cost_text} of tracked spend, {events_without} event(s) -- had nothing "
            "that repeated at all, so nothing appears for them in the buckets below. "
            "That's not a gap in the data; every call in those tasks was simply unique."
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
            f"one near-duplicate call ({len(near_pairs)} such pair(s) total) -- similar, "
            "not identical, arguments to an earlier call on the same execution path. A "
            "separate, lower-confidence signal from the exact-match note above; see the "
            "near_duplicate bucket below."
        )
