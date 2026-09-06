"""A minimal report renderer: one CSV row per bucket. Registered as a
plain function, not a class -- see redundo's own report_formats.py for
why a renderer is Strategy-via-callable, not a class hierarchy: there's
no shared behavior across renderers worth a base class.
"""

from __future__ import annotations

from redundo.analyzer import AnalysisResult


def to_csv(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    lines = ["bucket,count,cost_usd,tokens_in,tokens_out"]
    for bucket in result.buckets:
        s = bucket.slice
        lines.append(f"{bucket.key},{s.count},{s.cost_usd:.6f},{s.tokens_in},{s.tokens_out}")
    return "\n".join(lines) + "\n"
