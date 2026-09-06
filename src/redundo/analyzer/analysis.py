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
reasons for spot-checking, and an optional closing footnote. `report.py`'s
renderers depend only on this shape -- they don't know what a `Verdict`
is, or what "waste" means; any conforming analysis gets full text/json/html
rendering for free.
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
                "extra_notes": list(self.coverage.extra_notes),
            },
            "by_bucket": {
                b.key: {"label": b.label, "rule_text": b.rule_text, **slice_dict(b.slice)}
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
            "footnote": self.footnote,
        }


class Analysis(ABC):
    name: str

    @abstractmethod
    def run(self, events: list[Event]) -> AnalysisResult: ...
