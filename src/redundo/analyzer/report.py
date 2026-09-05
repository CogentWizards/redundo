"""Render a Report as text, JSON, or a self-contained HTML page."""

from __future__ import annotations

import html
import json

from .classify import Verdict
from .metrics import CoverageStats, Report, Slice

_ORDER = (Verdict.CONFIRMED_WASTE, Verdict.LIKELY_LEGITIMATE, Verdict.UNCLASSIFIED)

# The rule itself, printed next to every count rather than left implicit.
# "42 confirmed_waste" is a claim; "42 confirmed_waste -- repeated call,
# unchanged result, no intervening write, task failed" is a claim someone
# can check against one case by hand. That's what makes it credible enough
# to forward. Keep this in sync with classify.py's actual decision logic --
# it's prose describing that logic, not a separate source of truth.
RULE_TEXT = {
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


def to_json(report: Report, *, indent: int = 2) -> str:
    return json.dumps(report.as_dict(), indent=indent)


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
    if coverage.events_in_tasks_with_no_candidate_pairs:
        cmp_pct = coverage.tasks_with_candidate_pairs_fraction * 100
        lines.append(
            f"  {coverage.tasks_with_candidate_pairs}/{coverage.tasks_total} tasks "
            f"({cmp_pct:.0f}%) had at least one repeated call for redundancy detection "
            f"to examine. The rest -- {_fmt_usd(coverage.cost_usd_in_tasks_with_no_candidate_pairs)} "
            f"of tracked spend, {coverage.events_in_tasks_with_no_candidate_pairs} event(s) -- "
            "had nothing that repeated at all, so nothing appears for them in the buckets "
            "below. That's not a gap in the data; every call in those tasks was simply unique."
        )
    return lines


def to_text(report: Report) -> str:
    lines: list[str] = []
    lines.extend(_coverage_lines(report.coverage))
    lines.append("")
    lines.append(f"Candidate redundant-repeat pairs: {report.total_candidate_pairs}")
    lines.append("")

    for verdict in _ORDER:
        s = report.by_verdict[verdict]
        lines.append(f"{s.count} {verdict.value} -- {RULE_TEXT[verdict]}")
        lines.append(f"  cost_usd:   {s.cost_usd:.6f}" + (
            f"  ({s.unpriced_count} repeat(s) had no cost_usd)" if s.unpriced_count else ""
        ))
        lines.append(f"  tokens_in:  {s.tokens_in}")
        lines.append(f"  tokens_out: {s.tokens_out}")

        by_model = report.by_verdict_and_model.get(verdict, {})
        if by_model:
            lines.append("  by model:")
            for model, ms in sorted(by_model.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {model}: count={ms.count} cost_usd={ms.cost_usd:.6f} "
                    f"tokens_in={ms.tokens_in} tokens_out={ms.tokens_out}"
                )

        by_workflow = report.by_verdict_and_workflow.get(verdict, {})
        if by_workflow:
            lines.append("  by workflow:")
            for wf, ws in sorted(by_workflow.items(), key=lambda kv: -kv[1].count):
                lines.append(
                    f"    {wf}: count={ws.count} cost_usd={ws.cost_usd:.6f} "
                    f"tokens_in={ws.tokens_in} tokens_out={ws.tokens_out}"
                )

        reasons = report.reasons.get(verdict, [])
        if reasons:
            lines.append("  sample cases (spot-check these by hand):")
            for reason in reasons:
                lines.append(f"    - {reason}")

        lines.append("")

    if report.by_verdict[Verdict.UNCLASSIFIED].count > report.by_verdict[
        Verdict.CONFIRMED_WASTE
    ].count + report.by_verdict[Verdict.LIKELY_LEGITIMATE].count:
        lines.append(
            "Note: most candidate pairs are unclassified. That means the trace is "
            "missing signal (write flags, result correlation, or terminal outcome), "
            "not that this tool is being conservative for its own sake -- see README."
        )

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
# ---------------------------------------------------------------------------

_VERDICT_LABEL = {
    Verdict.CONFIRMED_WASTE: "Confirmed waste",
    Verdict.LIKELY_LEGITIMATE: "Likely legitimate",
    Verdict.UNCLASSIFIED: "Unclassified",
}

_VERDICT_RAMP = {
    # (light fill, light stroke, light title, light subtitle,
    #  dark fill, dark stroke, dark title, dark subtitle)
    Verdict.CONFIRMED_WASTE: ("#FAECE7", "#D85A30", "#4A1B0C", "#712B13",
                               "#712B13", "#F0997B", "#F5C4B3", "#F0997B"),
    Verdict.LIKELY_LEGITIMATE: ("#EAF3DE", "#639922", "#173404", "#27500A",
                                 "#27500A", "#97C459", "#C0DD97", "#97C459"),
    Verdict.UNCLASSIFIED: ("#F1EFE8", "#5F5E5A", "#2C2C2A", "#444441",
                            "#444441", "#B4B2A9", "#D3D1C7", "#B4B2A9"),
}


def _bar_chart_svg(report: Report) -> str:
    """One horizontal bar per bucket. Uses cost_usd if any bucket has priced
    repeats, otherwise falls back to call count -- an all-zero dollar chart
    would just be misleading.
    """
    slices = [(v, report.by_verdict[v]) for v in _ORDER]
    use_cost = any(s.cost_usd > 0 for _, s in slices)
    values = [s.cost_usd if use_cost else s.count for _, s in slices]
    max_value = max(values) or 1

    width, row_h, gap, label_w, chart_w = 640, 40, 14, 150, 400
    height = len(slices) * (row_h + gap) - gap + 8
    bars: list[str] = []

    for i, (verdict, s) in enumerate(slices):
        light_fill, light_stroke, *_ = _VERDICT_RAMP[verdict]
        y = i * (row_h + gap) + 4
        bar_w = (values[i] / max_value) * chart_w if max_value else 0
        value_text = _fmt_usd(s.cost_usd) if use_cost else f"{s.count} pair(s)"
        bars.append(
            f'<g>'
            f'<text x="0" y="{y + row_h / 2 + 5}" class="bar-label">{html.escape(_VERDICT_LABEL[verdict])}</text>'
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="{row_h}" class="bar-track" rx="4"/>'
            f'<rect x="{label_w}" y="{y}" width="{max(bar_w, 2)}" height="{row_h}" '
            f'fill="{light_fill}" stroke="{light_stroke}" stroke-width="1.5" rx="4"/>'
            f'<text x="{label_w + chart_w + 12}" y="{y + row_h / 2 + 5}" class="bar-value">{html.escape(value_text)}</text>'
            f'</g>'
        )

    axis_label = "Cost (USD)" if use_cost else "Candidate pairs (no cost_usd on any repeat)"
    return (
        f'<svg viewBox="0 0 {width} {height + 24}" width="100%" role="img" '
        f'aria-label="Bar chart comparing {axis_label.lower()} across the three classification buckets">'
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
    if coverage.events_in_tasks_with_no_candidate_pairs:
        cmp_pct = coverage.tasks_with_candidate_pairs_fraction * 100
        parts.append(
            f"<p>{coverage.tasks_with_candidate_pairs}/{coverage.tasks_total} tasks "
            f"(<strong>{cmp_pct:.0f}%</strong>) had at least one repeated call for "
            "redundancy detection to examine. The rest -- "
            f"{html.escape(_fmt_usd(coverage.cost_usd_in_tasks_with_no_candidate_pairs))} of "
            f"tracked spend, {coverage.events_in_tasks_with_no_candidate_pairs} event(s) -- "
            "had nothing that repeated at all, so nothing appears for them in the buckets "
            "below. That's not a gap in the data; every call in those tasks was simply "
            "unique.</p>"
        )
    return "".join(parts)


def _metric_card(verdict: Verdict, s: Slice) -> str:
    label = html.escape(_VERDICT_LABEL[verdict])
    headline = _fmt_usd(s.cost_usd) if s.cost_usd > 0 else f"{s.count} pair(s)"
    sub_parts = [f"{s.count} pair(s)"]
    if s.tokens_in or s.tokens_out:
        sub_parts.append(f"{s.tokens_in:,} in / {s.tokens_out:,} out tokens")
    if s.unpriced_count:
        sub_parts.append(f"{s.unpriced_count} unpriced")
    subtitle = html.escape(" &middot; ".join(sub_parts))
    return (
        f'<div class="card card-{verdict.value}">'
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


def _bucket_section(report: Report, verdict: Verdict) -> str:
    s = report.by_verdict[verdict]
    by_model = report.by_verdict_and_model.get(verdict, {})
    by_workflow = report.by_verdict_and_workflow.get(verdict, {})
    reasons = report.reasons.get(verdict, [])

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
        f'<section class="bucket bucket-{verdict.value}">'
        f'<h2>{html.escape(_VERDICT_LABEL[verdict])} <span class="count-pill">{s.count}</span></h2>'
        f'<p class="rule">{html.escape(RULE_TEXT[verdict])}</p>'
        f'{tables_html}'
        f'{reasons_html}'
        f'</section>'
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Waste analysis report</title>
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
.cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 2rem; }}
.card {{ background: var(--card-bg); border-radius: 12px; padding: 1rem 1.1rem; border-left: 3px solid; }}
.card-label {{ font-size: 13px; color: var(--text-secondary); margin: 0 0 6px; }}
.card-headline {{ font-size: 22px; font-weight: 500; margin: 0 0 4px; }}
.card-sub {{ font-size: 12px; color: var(--text-muted); margin: 0; }}
.card-confirmed_waste {{ border-color: #D85A30; }}
.card-likely_legitimate {{ border-color: #639922; }}
.card-unclassified {{ border-color: #5F5E5A; }}
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
<h1>Waste analysis report</h1>
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


def to_html(report: Report) -> str:
    """Render a self-contained HTML page: no CDN assets, no webfonts, no JS.
    Safe to open directly from disk or attach anywhere -- every value pulled
    from the trace is HTML-escaped before being interpolated.
    """
    coverage_html = _coverage_html(report.coverage)
    cards = "".join(_metric_card(v, report.by_verdict[v]) for v in _ORDER)
    chart = _bar_chart_svg(report)
    sections = "".join(_bucket_section(report, v) for v in _ORDER)

    waste = report.by_verdict[Verdict.CONFIRMED_WASTE].count
    legit = report.by_verdict[Verdict.LIKELY_LEGITIMATE].count
    unclassified = report.by_verdict[Verdict.UNCLASSIFIED].count
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

    return _HTML_TEMPLATE.format(
        total_pairs=report.total_candidate_pairs,
        coverage=coverage_html,
        cards=cards,
        chart=chart,
        sections=sections,
        footnote=html.escape(footnote),
    )
