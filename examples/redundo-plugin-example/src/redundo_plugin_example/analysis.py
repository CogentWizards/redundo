"""A minimal Analysis: one bucket, the total event count. No candidate
pairs, no classification -- just enough to show that a conforming
AnalysisResult gets full text/json/html rendering for free, without this
package writing a single line of rendering code itself.
"""

from __future__ import annotations

from redundo.analyzer import Analysis, AnalysisResult, Bucket, Slice
from redundo.analyzer.metrics import compute_generic_coverage
from redundo.analyzer.schema import Event


class CountAnalysis(Analysis):
    name = "count"

    def run(self, events: list[Event]) -> AnalysisResult:
        coverage = compute_generic_coverage(events)
        total = Slice()
        for event in events:
            total.add(event)

        return AnalysisResult(
            coverage=coverage,
            buckets=[
                Bucket(
                    key="all_events",
                    label="All events",
                    rule_text="every event in the loaded corpus, no filtering at all",
                    slice=total,
                )
            ],
            total_candidates=len(events),
            analysis_name=self.name,
        )
