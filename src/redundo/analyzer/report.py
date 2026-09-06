"""Render an AnalysisResult as text, JSON, or a self-contained HTML page.

Generic over any analysis's output -- this module doesn't know what a
`Verdict` is, or what "waste" means. It only knows the `AnalysisResult`/
`Bucket`/`Slice`/`CoverageStats` shapes from `analysis.py`/`metrics.py`.
Any analysis that produces a conforming `AnalysisResult` gets all three
renderers for free.
"""

from __future__ import annotations

import html
import json

from .analysis import AnalysisResult, Bucket
from .metrics import CoverageStats, Slice


def to_json(result: AnalysisResult, *, max_reasons: int = 20, indent: int = 2) -> str:
    data = result.as_dict()
    data["reasons"] = {key: reasons[:max_reasons] for key, reasons in result.reasons.items()}
    return json.dumps(data, indent=indent)


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def _coverage_lines(coverage: CoverageStats) -> list[str]:
    if coverage.total_events == 0:
        return ["Coverage: no events loaded."]

    pct = coverage.pricing_coverage_fraction * 100
    lines = [
        f"Coverage: {coverage.priced_events}/{coverage.total_events} events priced "
        f"({pct:.0f}%) -- {_fmt_usd(coverage.total_priced_cost_usd)} of tracked spend "
        "is what this analysis actually covers."
    ]
    if coverage.unpriced_events:
        lines.append(
            f"  {coverage.unpriced_events} event(s) had no cost_usd and are excluded "
            "from every dollar figure below -- the percentages are computed on the "
            "priced subset, not your total spend."
        )
    conf = coverage.task_id_confidence_fraction
    if conf is not None:
        lines.append(
            f"  Task-id confidence: {coverage.events_confident_task_id}/"
            f"{coverage.events_with_task_id_source_reported} events grouped by a real "
            f"conversation id ({conf * 100:.0f}%); the rest fell back to trace-id "
            "grouping, where cross-trace rework isn't detected."
        )
    for note in coverage.extra_notes:
        lines.append(f"  {note}")
    return lines


def to_text(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    lines: list[str] = []
    lines.extend(_coverage_lines(result.coverage))
    lines.append("")
    lines.append(f"Candidate redundant-repeat pairs: {result.total_candidates}")
    lines.append("")

    for bucket in result.buckets:
        s = bucket.slice
        lines.append(f"{s.count} {bucket.key} -- {bucket.rule_text}")
        lines.append(f"  cost_usd:   {s.cost_usd:.6f}" + (
            f"  ({s.unpriced_count} repeat(s) had no cost_usd)" if s.unpriced_count else ""
        ))
        lines.append(f"  tokens_in:  {s.tokens_in}")
        lines.append(f"  tokens_out: {s.tokens_out}")

        by_model = result.by_bucket_and_model.get(bucket.key, {})
        if by_model:
            lines.append("  by model:")
            for model, ms in sorted(by_model.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {model}: count={ms.count} cost_usd={ms.cost_usd:.6f} "
                    f"tokens_in={ms.tokens_in} tokens_out={ms.tokens_out}"
                )

        by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
        if by_workflow:
            lines.append("  by workflow:")
            for wf, ws in sorted(by_workflow.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {wf}: count={ws.count} cost_usd={ws.cost_usd:.6f} "
                    f"tokens_in={ws.tokens_in} tokens_out={ws.tokens_out}"
                )

        reasons = result.reasons.get(bucket.key, [])[:max_reasons]
        if reasons:
            lines.append("  sample cases (spot-check these by hand):")
            for reason in reasons:
                lines.append(f"    - {reason}")

        lines.append("")

    if result.footnote:
        lines.append(result.footnote)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
#
# Self-contained: no CDN, no webfonts, no JS framework. This has to run on
# someone's own trace data, possibly offline, and possibly years from now --
# a network dependency is a bug waiting to happen. Colors and chart tokens
# below are lifted from Anthropic's own design system so the page doesn't
# look like a hand-rolled report tool, but every value is inlined; nothing
# is fetched.
#
# Every string interpolated from the trace (model names, workflow labels,
# classification reasons -- all attacker-controlled if the trace comes from
# somewhere untrusted) goes through html.escape(). This file gets opened in
# a real browser; unescaped trace content would be a stored-XSS vector.
#
# Bucket color is assigned BY POSITION in result.buckets, not by key --
# an analysis can have any number of buckets with any keys, so there's no
# fixed enum to hang a color mapping off. Colors are applied via inline
# `style=`, not a `.card-{key}` CSS class: a hardcoded class-per-key
# mapping is exactly the kind of hand-written string that silently drifts
# out of sync with real bucket keys (this file used to have one, matched
# only to the waste analysis's three Verdict values).
# ---------------------------------------------------------------------------

_PALETTE: tuple[tuple[str, ...], ...] = (
    # (light fill, light stroke, light title, light subtitle,
    #  dark fill, dark stroke, dark title, dark subtitle)
    ("#FAECE7", "#D85A30", "#4A1B0C", "#712B13", "#712B13", "#F0997B", "#F5C4B3", "#F0997B"),
    ("#EAF3DE", "#639922", "#173404", "#27500A", "#27500A", "#97C459", "#C0DD97", "#97C459"),
    ("#F1EFE8", "#5F5E5A", "#2C2C2A", "#444441", "#444441", "#B4B2A9", "#D3D1C7", "#B4B2A9"),
)


def _palette_for(index: int) -> tuple[str, ...]:
    return _PALETTE[index % len(_PALETTE)]


def _bar_chart_svg(buckets: list[Bucket]) -> str:
    """One horizontal bar per bucket. Uses cost_usd if any bucket has priced
    repeats, otherwise falls back to call count -- an all-zero dollar chart
    would just be misleading.
    """
    use_cost = any(b.slice.cost_usd > 0 for b in buckets)
    values = [b.slice.cost_usd if use_cost else b.slice.count for b in buckets]
    max_value = max(values) or 1

    width, row_h, gap, label_w, chart_w = 640, 40, 14, 150, 400
    height = len(buckets) * (row_h + gap) - gap + 8
    bars: list[str] = []

    for i, bucket in enumerate(buckets):
        light_fill, light_stroke, *_ = _palette_for(i)
        y = i * (row_h + gap) + 4
        bar_w = (values[i] / max_value) * chart_w if max_value else 0
        value_text = _fmt_usd(bucket.slice.cost_usd) if use_cost else f"{bucket.slice.count} pair(s)"
        bars.append(
            f'<g>'
            f'<text x="0" y="{y + row_h / 2 + 5}" class="bar-label">{html.escape(bucket.label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="{row_h}" class="bar-track" rx="4"/>'
            f'<rect x="{label_w}" y="{y}" width="{max(bar_w, 2)}" height="{row_h}" '
            f'fill="{light_fill}" stroke="{light_stroke}" stroke-width="1.5" rx="4"/>'
            f'<text x="{label_w + chart_w + 12}" y="{y + row_h / 2 + 5}" class="bar-value">{html.escape(value_text)}</text>'
            f'</g>'
        )

    axis_label = "Cost (USD)" if use_cost else "Candidate pairs (no cost_usd on any repeat)"
    return (
        f'<svg viewBox="0 0 {width} {height + 24}" width="100%" role="img" '
        f'aria-label="Bar chart comparing {axis_label.lower()} across the classification buckets">'
        f'<title>{html.escape(axis_label)} by bucket</title>'
        f'{"".join(bars)}'
        f'<text x="{label_w}" y="{height + 20}" class="axis-label">{html.escape(axis_label)}</text>'
        f'</svg>'
    )


def _coverage_html(coverage: CoverageStats) -> str:
    if coverage.total_events == 0:
        return "<p>Coverage: no events loaded.</p>"

    pct = coverage.pricing_coverage_fraction * 100
    parts = [
        f"<p>Coverage: <strong>{coverage.priced_events}/{coverage.total_events} events "
        f"priced ({pct:.0f}%)</strong> -- <strong>{html.escape(_fmt_usd(coverage.total_priced_cost_usd))}"
        "</strong> of tracked spend is what this analysis actually covers.</p>"
    ]
    if coverage.unpriced_events:
        parts.append(
            f"<p>{coverage.unpriced_events} event(s) had no cost_usd and are excluded "
            "from every dollar figure below -- the percentages are computed on the "
            "priced subset, not your total spend.</p>"
        )
    conf = coverage.task_id_confidence_fraction
    if conf is not None:
        parts.append(
            f"<p>Task-id confidence: {coverage.events_confident_task_id}/"
            f"{coverage.events_with_task_id_source_reported} events grouped by a real "
            f"conversation id (<strong>{conf * 100:.0f}%</strong>); the rest fell back to "
            "trace-id grouping, where cross-trace rework isn't detected.</p>"
        )
    for note in coverage.extra_notes:
        parts.append(f"<p>{html.escape(note)}</p>")
    return "".join(parts)


def _metric_card(bucket: Bucket, index: int) -> str:
    _, stroke, *_ = _palette_for(index)
    s = bucket.slice
    label = html.escape(bucket.label)
    headline = _fmt_usd(s.cost_usd) if s.cost_usd > 0 else f"{s.count} pair(s)"
    sub_parts = [f"{s.count} pair(s)"]
    if s.tokens_in or s.tokens_out:
        sub_parts.append(f"{s.tokens_in:,} in / {s.tokens_out:,} out tokens")
    if s.unpriced_count:
        sub_parts.append(f"{s.unpriced_count} unpriced")
    subtitle = html.escape(" &middot; ".join(sub_parts))
    return (
        f'<div class="card" style="border-left-color: {stroke};">'
        f'<p class="card-label">{label}</p>'
        f'<p class="card-headline">{html.escape(headline)}</p>'
        f'<p class="card-sub">{subtitle}</p>'
        f'</div>'
    )


def _breakdown_table(title: str, rows: dict[str, Slice]) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<tr><td>{html.escape(key)}</td><td class="num">{s.count}</td>'
        f'<td class="num">{html.escape(_fmt_usd(s.cost_usd))}</td>'
        f'<td class="num">{s.tokens_in:,}</td><td class="num">{s.tokens_out:,}</td></tr>'
        for key, s in sorted(rows.items(), key=lambda kv: -kv[1].count)
    )
    return (
        f'<table class="breakdown"><caption>{html.escape(title)}</caption>'
        f'<thead><tr><th>{html.escape(title)}</th><th class="num">Count</th>'
        f'<th class="num">Cost</th><th class="num">Tokens in</th><th class="num">Tokens out</th></tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _bucket_section(result: AnalysisResult, bucket: Bucket, *, max_reasons: int) -> str:
    by_model = result.by_bucket_and_model.get(bucket.key, {})
    by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
    reasons = result.reasons.get(bucket.key, [])[:max_reasons]

    reasons_html = ""
    if reasons:
        items = "".join(f"<li>{html.escape(r)}</li>" for r in reasons)
        reasons_html = (
            '<details class="reasons"><summary>Sample cases '
            f'(spot-check these by hand, {len(reasons)} shown)</summary>'
            f'<ul>{items}</ul></details>'
        )

    tables = (
        _breakdown_table("By model", by_model) + _breakdown_table("By workflow", by_workflow)
    )
    tables_html = f'<div class="tables">{tables}</div>' if tables else ""

    return (
        f'<section class="bucket">'
        f'<h2>{html.escape(bucket.label)} <span class="count-pill">{bucket.slice.count}</span></h2>'
        f'<p class="rule">{html.escape(bucket.rule_text)}</p>'
        f'{tables_html}'
        f'{reasons_html}'
        f'</section>'
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis report</title>
<style>
:root {{
  --page-bg: #ffffff; --card-bg: #fcfcfb; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --border: rgba(11,11,11,0.10);
  --gridline: #e1e0d9;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --page-bg: #151514; --card-bg: #1a1a19; --text-primary: #f0efec;
    --text-secondary: #c3c2b7; --text-muted: #898781; --border: rgba(255,255,255,0.10);
    --gridline: #2c2c2a;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  background: var(--page-bg); color: var(--text-primary);
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 16px; line-height: 1.6; margin: 0; padding: 2.5rem 1.5rem 4rem;
}}
main {{ max-width: 760px; margin: 0 auto; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 4px; }}
h2 {{ font-size: 18px; font-weight: 500; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }}
.subtitle {{ color: var(--text-secondary); font-size: 14px; margin: 0 0 2rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 2rem; }}
.card {{ background: var(--card-bg); border-radius: 12px; padding: 1rem 1.1rem; border-left: 3px solid; }}
.card-label {{ font-size: 13px; color: var(--text-secondary); margin: 0 0 6px; }}
.card-headline {{ font-size: 22px; font-weight: 500; margin: 0 0 4px; }}
.card-sub {{ font-size: 12px; color: var(--text-muted); margin: 0; }}
.chart {{ margin: 0 0 2.5rem; }}
.bar-label {{ font-size: 13px; fill: var(--text-secondary); }}
.bar-value {{ font-size: 13px; fill: var(--text-primary); }}
.axis-label {{ font-size: 12px; fill: var(--text-muted); }}
.bar-track {{ fill: var(--gridline); opacity: 0.5; }}
.bucket {{ padding: 1.5rem 0; border-top: 1px solid var(--border); }}
.count-pill {{
  font-size: 13px; font-weight: 500; padding: 2px 10px; border-radius: 999px;
  background: var(--gridline); color: var(--text-secondary);
}}
.rule {{ font-size: 13px; color: var(--text-secondary); margin: -6px 0 1rem; }}
.coverage {{
  background: var(--card-bg); border-radius: 12px; padding: 0.9rem 1.1rem;
  font-size: 13px; color: var(--text-secondary); margin: 0 0 1.5rem;
}}
.coverage p {{ margin: 0 0 4px; }}
.coverage p:last-child {{ margin-bottom: 0; }}
.coverage strong {{ color: var(--text-primary); font-weight: 500; }}
.tables {{ display: flex; flex-wrap: wrap; gap: 24px; margin-bottom: 1rem; }}
table.breakdown {{ border-collapse: collapse; font-size: 13px; flex: 1; min-width: 260px; }}
table.breakdown caption {{ display: none; }}
table.breakdown th, table.breakdown td {{ padding: 6px 10px 6px 0; text-align: left; border-bottom: 1px solid var(--border); }}
table.breakdown th {{ color: var(--text-muted); font-weight: 500; }}
table.breakdown td.num, table.breakdown th.num {{ text-align: right; padding-right: 4px; }}
details.reasons {{ font-size: 13px; color: var(--text-secondary); }}
details.reasons summary {{ cursor: pointer; color: var(--text-primary); margin-bottom: 8px; }}
details.reasons ul {{ margin: 0; padding-left: 1.2rem; }}
details.reasons li {{ margin-bottom: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
.footnote {{
  margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid var(--border);
  font-size: 13px; color: var(--text-secondary);
}}
</style>
</head>
<body>
<main>
<h1>Analysis report</h1>
<p class="subtitle">{total_pairs} candidate redundant-repeat pair(s) evaluated</p>
<div class="coverage">{coverage}</div>
<div class="cards">{cards}</div>
<div class="chart">{chart}</div>
{sections}
<p class="footnote">{footnote}</p>
</main>
</body>
</html>
"""


def to_html(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render a self-contained HTML page: no CDN assets, no webfonts, no JS.
    Safe to open directly from disk or attach anywhere -- every value pulled
    from the trace is HTML-escaped before being interpolated.
    """
    coverage_html = _coverage_html(result.coverage)
    cards = "".join(_metric_card(b, i) for i, b in enumerate(result.buckets))
    chart = _bar_chart_svg(result.buckets)
    sections = "".join(
        _bucket_section(result, b, max_reasons=max_reasons) for b in result.buckets
    )

    return _HTML_TEMPLATE.format(
        total_pairs=result.total_candidates,
        coverage=coverage_html,
        cards=cards,
        chart=chart,
        sections=sections,
        footnote=html.escape(result.footnote or ""),
    )
