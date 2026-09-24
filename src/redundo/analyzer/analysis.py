"""The contract every analysis implements -- built-in or third-party.

Unlike an adapter source, an analysis doesn't need to recognize anything:
every analysis runs unconditionally over whatever events got loaded, so
there's no `detect()` here, just `run()`. This is the Strategy pattern --
interchangeable algorithms behind one method, picked by the caller
(`--analysis waste` vs. a plugin's own name) -- not Template Method the
way `SpanBasedAdapterSource`-shaped sources are; there's no shared
step-by-step algorithm across analyses worth hoisting into a base class,
since a cost-anomaly analysis and a redundant-call analysis have no
common internal shape, only a common *output* shape.

That output shape is `AnalysisResult`: a coverage section, an ordered list
of `Bucket`s (each just a label, a human-readable rule, and a `Slice` of
count/cost/tokens -- generalized from what used to be a `Verdict`-keyed
`Report`), per-model/per-workflow breakdowns of those buckets, sample
reasons for spot-checking, an optional ranked list of highlights, and an
optional closing footnote. `report.py`'s renderers depend only on this
shape -- they don't know what a `Verdict` is, or what "waste" means; any
conforming analysis gets full text/json/html rendering for free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .metrics import CoverageStats, Slice
from .schema import Event


@dataclass
class Bucket:
    key: str
    label: str
    rule_text: str
    slice: Slice
    # A short, prescriptive "what to do about this" line -- optional, and
    # deliberately NOT required: it's specific advice about what a bucket
    # *means*, which only the analysis that defines the bucket can write.
    # Renderers must treat an absent action_text as "nothing to show", not
    # a gap to fill in -- that's what keeps report.py generic over any
    # analysis, not just the one that happens to set this.
    action_text: str | None = None
    # A short, interpretive framing line -- optional, distinct from
    # action_text (which prescribes what to do) and rule_text (which
    # states the evidence rule). This is for a bucket-specific reading of
    # what the finding actually means, when the analysis has one worth
    # saying. Absent means nothing to show, same convention as action_text.
    insight_text: str | None = None
    # An opaque grouping key -- optional, and NOT a second taxonomy
    # report.py understands. WasteAnalysis's six buckets answer two
    # different questions (did this repeat get a verdict, versus does
    # this repeat merely resemble something), and listing all six flat
    # under one heading makes them look like one mutually-exclusive
    # dimension when they aren't. When buckets set this, report.py
    # renders one section per distinct group (first-seen order) instead
    # of one flat list, using AnalysisResult.group_descriptions for each
    # section's own subheading. Buckets that leave this unset (or every
    # bucket in a conforming AnalysisResult, for any analysis that
    # doesn't use this) all render in one flat section, unchanged.
    group: str | None = None


@dataclass
class AnalysisResult:
    coverage: CoverageStats
    buckets: list[Bucket] = field(default_factory=list)
    by_bucket_and_model: dict[str, dict[str, Slice]] = field(default_factory=dict)
    by_bucket_and_workflow: dict[str, dict[str, Slice]] = field(default_factory=dict)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    total_candidates: int = 0
    analysis_name: str = ""
    footnote: str | None = None
    # Pre-rendered, ranked, human-readable strings: the specific things
    # worth acting on first, in priority order. Optional and deliberately
    # opaque to report.py -- an analysis populates this only when it has
    # a real, defensible way to rank (e.g. WasteAnalysis ranks confirmed
    # waste by its own real cost_usd, never a projection or a guess), and
    # leaves it empty otherwise rather than inventing an order. Renderers
    # show nothing when this is empty, same convention as footnote.
    highlights: list[str] = field(default_factory=list)
    # A pre-rendered (label, value, sub) headline stat, optional and
    # opaque to report.py, same convention as highlights above. When
    # present, this is what a reader sees FIRST as the report's own
    # confidence signal -- report.py shows it in place of the generic,
    # cost-coverage-only "Trace coverage" stat cell, since a dollar
    # coverage figure isn't the right headline for an analysis whose
    # real claim is "did we reach a real verdict," not "how much did we
    # price." WasteAnalysis computes it from its own three real Verdict
    # buckets (confirmed_waste/likely_legitimate/unclassified); leaves it
    # None when there's nothing to compute a confidence figure from
    # (zero exact-match candidate pairs) rather than inventing one.
    confidence_stat: tuple[str, str, str] | None = None
    # One-sentence subheading per distinct Bucket.group value, keyed by
    # that same opaque group string. Only consulted when at least one
    # bucket sets .group; a group with no entry here just renders with
    # no subheading rather than report.py inventing one.
    group_descriptions: dict[str, str] = field(default_factory=dict)

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
            "analysis": self.analysis_name,
            "total_candidates": self.total_candidates,
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
                "synthesized_cost_only_events": self.coverage.synthesized_cost_only_events,
                "synthesized_cost_only_usd": round(self.coverage.synthesized_cost_only_usd, 6),
                "total_call_events": self.coverage.total_call_events,
                "cost_by_basis": {
                    basis: {"events": stat.events, "usd": round(stat.usd, 6)}
                    for basis, stat in self.coverage.cost_by_basis.items()
                },
                "source_path": self.coverage.source_path,
                "extra_notes": list(self.coverage.extra_notes),
            },
            "confidence_stat": (
                {"label": self.confidence_stat[0], "value": self.confidence_stat[1],
                 "sub": self.confidence_stat[2]}
                if self.confidence_stat else None
            ),
            "by_bucket": {
                b.key: {"label": b.label, "rule_text": b.rule_text, "group": b.group,
                         **slice_dict(b.slice)}
                for b in self.buckets
            },
            "by_bucket_and_model": {
                key: {model: slice_dict(s) for model, s in models.items()}
                for key, models in self.by_bucket_and_model.items()
            },
            "by_bucket_and_workflow": {
                key: {wf: slice_dict(s) for wf, s in wfs.items()}
                for key, wfs in self.by_bucket_and_workflow.items()
            },
            "group_descriptions": dict(self.group_descriptions),
            "highlights": list(self.highlights),
            "footnote": self.footnote,
        }


class Analysis(ABC):
    name: str

    @abstractmethod
    def run(self, events: list[Event]) -> AnalysisResult: ...
