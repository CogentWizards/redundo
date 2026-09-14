"""Render an AnalysisResult as text, JSON, or a self-contained HTML page.

Generic over any analysis's output -- this module doesn't know what a
`Verdict` is, or what "waste" means. It only knows the `AnalysisResult`/
`Bucket`/`Slice`/`CoverageStats` shapes from `analysis.py`/`metrics.py`.
Any analysis that produces a conforming `AnalysisResult` gets all three
renderers for free. A `Bucket.action_text`, when an analysis sets one, is
the one place bucket-specific prose can reach the HTML report without
this module knowing what it means; see analysis.py's own docstring on
that field for why it's optional.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from urllib.parse import quote

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
# someone's own trace data, possibly offline, and possibly years from now.
# A network dependency is a bug waiting to happen. The serif display font
# and oklch color tokens below are all system/fallback stacks and inline
# CSS values, never fetched. The CogentWizards org mark embedded below is
# a small PNG (base64, ~6 KB), not a fetch either.
#
# Every string interpolated from the trace (model names, workflow labels,
# classification reasons, all attacker-controlled if the trace comes from
# somewhere untrusted) goes through html.escape(). This file gets opened in
# a real browser; unescaped trace content would be a stored-XSS vector.
#
# Bucket color is assigned BY POSITION in result.buckets, not by key. An
# analysis can have any number of buckets with any keys, so there's no
# fixed enum to hang a color mapping off.
#
# All dollar figures in this report are USD, always -- that's a property
# of cost_usd itself (every source computes it from a provider's own
# USD-denominated rate), not a display default. This renderer never
# converts currency: a live exchange-rate fetch would contradict the
# "nothing is fetched" rule above, and a baked-in static rate would go
# stale and misrepresent the number. See the explicit "amounts in USD"
# line in the rendered page for how that's surfaced instead of guessed at.
#
# "Interactive" here means CSS-only: the theme switch, the model/workflow
# tabs inside each bucket, and the collapsible sections are all built from
# hidden radio/checkbox inputs and sibling selectors, with :has() where a
# plain sibling selector can't reach far enough. No inline event handler,
# no <script> tag, anywhere in this file.
# ---------------------------------------------------------------------------

# (accent, accent-fill-light, accent-fill-dark) -- oklch, cycled by bucket
# POSITION the same way the old hex _PALETTE was. accent-fill is the bar's
# own background tint; accent is its border/dot/text color.
_PALETTE: tuple[tuple[str, str, str], ...] = (
    ("oklch(0.55 0.15 38)", "oklch(0.93 0.035 38)", "oklch(0.32 0.055 38)"),
    ("oklch(0.55 0.13 142)", "oklch(0.93 0.032 142)", "oklch(0.32 0.05 142)"),
    ("oklch(0.55 0.012 80)", "oklch(0.93 0.004 80)", "oklch(0.30 0.006 80)"),
    ("oklch(0.55 0.14 258)", "oklch(0.93 0.033 258)", "oklch(0.32 0.05 258)"),
)

_REPO_URL = "https://github.com/CogentWizards/redundo"

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
)

# The CogentWizards org mark, downscaled to 64x64 and re-encoded. Embedded
# as data so the header logo needs no network request, matching this
# file's own "nothing is fetched" rule.
_LOGO_BASE64 = (
    "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"
)


def _redundo_version() -> str:
    try:
        return _pkg_version("redundo")
    except PackageNotFoundError:
        return "dev"


def _mailto_share_href() -> str:
    subject = "redundo waste report"
    body = (
        "Sharing a redundo waste-detection report (attached).\n\n"
        f"redundo is open source: {_REPO_URL}"
    )
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"


def _palette_for(index: int) -> tuple[str, str, str]:
    return _PALETTE[index % len(_PALETTE)]


def _number_word(n: int) -> str:
    if 0 <= n <= 20:
        return _NUMBER_WORDS[n]
    return f"{n:,}"


def _headline(result: AnalysisResult) -> tuple[str, str]:
    """The report's opening sentence: how many pairs were evaluated, and
    how many landed in the analysis's own most prominent bucket (position
    0 in result.buckets -- the analysis orders its own buckets, this
    renderer just trusts that order, same as everywhere else it uses
    bucket position). Returns (eyebrow, headline) so the caller doesn't
    also have to know analysis_name's fallback.
    """
    eyebrow = html.escape(result.analysis_name or "Event analysis")
    total = result.total_candidates
    if total == 0 or not result.buckets:
        return eyebrow, "No repeated calls found."
    top = result.buckets[0]
    noun = "pair" if total == 1 else "pairs"
    verb = "was" if top.slice.count == 1 else "were"
    headline = (
        f"{_number_word(total).capitalize()} repeat {noun}. "
        f"{_number_word(top.slice.count).capitalize()} {verb} {html.escape(top.label.lower())}."
    )
    return eyebrow, headline


def _coverage_html(coverage: CoverageStats) -> str:
    if coverage.total_events == 0:
        return "<p>Coverage: no events loaded.</p>"

    pct = coverage.pricing_coverage_fraction * 100
    parts = [
        f"<p>{coverage.priced_events} of {coverage.total_events} events carried a price "
        f"({pct:.0f}%) -- <strong>{html.escape(_fmt_usd(coverage.total_priced_cost_usd))}</strong> "
        "of tracked spend is what this analysis actually covers. "
        "<strong>All amounts are USD</strong>, as reported by each event's own "
        "<code>cost_usd</code>; this report never converts or estimates a currency.</p>"
    ]
    if coverage.unpriced_events:
        parts.append(
            f"<p>{coverage.unpriced_events} event(s) had no <code>cost_usd</code> and are "
            "excluded from every dollar figure above and below -- percentages are computed "
            "on the priced subset, not the total.</p>"
        )
    conf = coverage.task_id_confidence_fraction
    if conf is not None:
        parts.append(
            f"<p>Task-id confidence: {coverage.events_confident_task_id} of "
            f"{coverage.events_with_task_id_source_reported} events grouped by a real "
            f"conversation id ({conf * 100:.0f}%); the rest fell back to trace-id grouping, "
            "where cross-trace rework isn't detected.</p>"
        )
    for note in coverage.extra_notes:
        parts.append(f"<p>{html.escape(note)}</p>")
    return "".join(parts)


def _spend_rows(buckets: list[Bucket]) -> str:
    """One row per bucket: a colored dot, label, count, an inline
    proportional bar, and a dollar (or pair-count) figure -- replaces what
    used to be a separate cards grid and SVG bar chart with a single list,
    each row linking to that bucket's own detail section below.
    """
    use_cost = any(b.slice.cost_usd > 0 for b in buckets)
    values = [b.slice.cost_usd if use_cost else b.slice.count for b in buckets]
    max_value = max(values) or 1

    rows = []
    for i, bucket in enumerate(buckets):
        accent, fill, _ = _palette_for(i)
        pct = max((values[i] / max_value) * 100, 1.5) if max_value else 0
        value_text = _fmt_usd(bucket.slice.cost_usd) if use_cost else f"{bucket.slice.count} pair(s)"
        anchor = html.escape(f"#bucket-{bucket.key}", quote=True)
        rows.append(f"""
<a class="row" href="{anchor}">
  <span class="row-name">
    <span class="dot" style="background:{accent}"></span>
    <span class="rowname">{html.escape(bucket.label)}</span>
    <span class="row-count">{bucket.slice.count}</span>
  </span>
  <span class="row-track"><span class="bar rowfill" style="width:{pct:.1f}%;background:{fill};border-left:2px solid {accent}"></span></span>
  <span class="row-value">{html.escape(value_text)}</span>
  <span class="rowgo">&rarr;</span>
</a>""")
    return "".join(rows)


def _breakdown_table(title: str, rows: dict[str, Slice]) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<tr><td class="rowkey">{html.escape(key)}</td><td class="num">{s.count}</td>'
        f'<td class="num">{html.escape(_fmt_usd(s.cost_usd))}</td>'
        f'<td class="num">{s.tokens_in:,}</td><td class="num">{s.tokens_out:,}</td></tr>'
        for key, s in sorted(rows.items(), key=lambda kv: -kv[1].count)
    )
    return (
        f'<div class="table-scroll"><table class="breakdown"><caption>{html.escape(title)}</caption>'
        f'<thead><tr><th>{html.escape(title)}</th><th class="num">Count</th>'
        f'<th class="num">Cost</th><th class="num">Tokens in</th><th class="num">Tokens out</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


def _breakdown_tabs(key: str, by_model: dict[str, Slice], by_workflow: dict[str, Slice]) -> str:
    """CSS-only tabs (hidden radio inputs, sibling selectors on a shared
    data-tab vocabulary) when both breakdowns are present, so they don't
    sit side by side and overflow a narrow viewport. Falls back to a
    single table, no tab UI, when only one is present.
    """
    model_table = _breakdown_table("By model", by_model)
    workflow_table = _breakdown_table("By workflow", by_workflow)
    if model_table and workflow_table:
        safe_key = html.escape(key, quote=True)
        return f"""
<div class="tabs">
  <input type="radio" name="tabs-{safe_key}" id="{safe_key}-tab-model" data-tab="model" checked>
  <input type="radio" name="tabs-{safe_key}" id="{safe_key}-tab-workflow" data-tab="workflow">
  <div class="tabstrip">
    <label class="tablabel" for="{safe_key}-tab-model">By model</label>
    <label class="tablabel" for="{safe_key}-tab-workflow">By workflow</label>
  </div>
  <div class="panel" data-tab="model">{model_table}</div>
  <div class="panel" data-tab="workflow">{workflow_table}</div>
</div>"""
    return model_table or workflow_table


def _bucket_section(result: AnalysisResult, bucket: Bucket, *, index: int, max_reasons: int) -> str:
    by_model = result.by_bucket_and_model.get(bucket.key, {})
    by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
    reasons = result.reasons.get(bucket.key, [])[:max_reasons]
    accent, *_ = _palette_for(index)

    reasons_html = ""
    if reasons:
        items = "".join(f'<li class="case">{html.escape(r)}</li>' for r in reasons)
        reasons_html = f"""
<details class="cases">
  <summary><span class="chev2">&#9656;</span> Sample cases to spot-check by hand ({len(reasons)})</summary>
  <ul class="caselist">{items}</ul>
</details>"""

    action_html = ""
    if bucket.action_text:
        action_html = (
            f'<p class="action"><span class="action-label">Action</span> '
            f'{html.escape(bucket.action_text)}</p>'
        )

    tabs_html = _breakdown_tabs(bucket.key, by_model, by_workflow)
    open_attr = " open" if index == 0 else ""
    safe_id = html.escape(f"bucket-{bucket.key}", quote=True)

    return f"""
<details class="bucket" id="{safe_id}"{open_attr}>
  <summary>
    <span class="bucket-head">
      <span class="chev">&#9656;</span>
      <span class="dot" style="background:{accent}"></span>
      <h3>{html.escape(bucket.label)}</h3>
      <span class="bucket-meta">{bucket.slice.count} pair(s) &middot; {html.escape(_fmt_usd(bucket.slice.cost_usd))}</span>
    </span>
  </summary>
  <div class="bucket-body">
    <div class="rule-block">
      <p class="rule">{html.escape(bucket.rule_text)}</p>
      {action_html}
    </div>
    {tabs_html}
    {reasons_html}
  </div>
</details>"""


def _header_html() -> str:
    logo_src = f"data:image/png;base64,{_LOGO_BASE64}"
    version = html.escape(_redundo_version())
    return f"""
<header class="page-header">
  <div class="brand">
    <img src="{logo_src}" width="24" height="24" alt="" class="brand-mark">
    <span class="brand-name">redundo</span>
    <span class="brand-version">v{version}</span>
  </div>
  <div class="sw noprint" role="group" aria-label="Theme">
    <input type="radio" name="theme" id="theme-auto" checked>
    <label for="theme-auto" title="Match system theme">Auto</label>
    <input type="radio" name="theme" id="theme-light">
    <label for="theme-light" title="Light theme">Light</label>
    <input type="radio" name="theme" id="theme-dark">
    <label for="theme-dark" title="Dark theme">Dark</label>
  </div>
</header>"""


def _footer_html() -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    version = html.escape(_redundo_version())
    mailto = _mailto_share_href()
    return f"""
<footer class="page-footer">
  <div>
    <p class="tagline">Generated with <strong>redundo</strong>. Point it at your agent's OTLP traces, get a report on what's wasted.</p>
    <p class="reassurance">Self-contained file. No data left your machine to produce it.</p>
  </div>
  <div class="noprint footer-links">
    <a class="quiet" href="{_REPO_URL}">GitHub</a>
    <a class="quiet" href="{_REPO_URL}/tree/main/docs">Docs</a>
    <a class="quiet" href="{_REPO_URL}/blob/main/LICENSE">License</a>
    <a class="quiet" href="{_REPO_URL}/issues">Report an issue</a>
  </div>
  <div class="footer-meta">
    <p class="noprint"><a class="quiet" href="{mailto}">Share by email</a>. Attach this file, it's the whole report.</p>
    <p class="meta">{generated} &middot; redundo v{version}</p>
  </div>
</footer>"""


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis report</title>
<style>
:root {{
  --bg:#fbfaf8; --panel:#ffffff; --ink:#111110; --ink2:#56534d; --ink3:#8d8a82;
  --line:rgba(17,17,16,0.11); --hair:rgba(17,17,16,0.055); --track:#edebe4;
  --sel:rgba(17,17,16,0.05);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#121211; --panel:#191918; --ink:#f2f0ec; --ink2:#b8b5ac; --ink3:#83807a;
    --line:rgba(255,255,255,0.12); --hair:rgba(255,255,255,0.06); --track:#262624;
    --sel:rgba(255,255,255,0.06);
  }}
}}
/* Manual override, wins over prefers-color-scheme either direction because
   a body:has() selector is more specific than a bare :root rule -- needed
   both ways, not just toward dark: with "Auto" the resting default already
   follows the system preference, so a system-dark reader needs an explicit
   path back to light too, not just a way to force dark on top of dark. */
body:has(#theme-light:checked) {{
  --bg:#fbfaf8; --panel:#ffffff; --ink:#111110; --ink2:#56534d; --ink3:#8d8a82;
  --line:rgba(17,17,16,0.11); --hair:rgba(17,17,16,0.055); --track:#edebe4;
  --sel:rgba(17,17,16,0.05);
}}
body:has(#theme-dark:checked) {{
  --bg:#121211; --panel:#191918; --ink:#f2f0ec; --ink2:#b8b5ac; --ink3:#83807a;
  --line:rgba(255,255,255,0.12); --hair:rgba(255,255,255,0.06); --track:#262624;
  --sel:rgba(255,255,255,0.06);
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 15px; line-height: 1.6; -webkit-font-smoothing: antialiased;
}}
::selection {{ background: var(--sel); }}
a {{ color: inherit; }}
code {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 0.92em; }}
.serif {{ font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif; }}
.page-header, .page-footer, main {{ max-width: 760px; margin: 0 auto; padding-left: 24px; padding-right: 24px; }}
.page-header {{
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; row-gap: 10px;
  padding-top: 22px; padding-bottom: 22px;
}}
.brand {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }}
.brand-mark {{ border-radius: 6px; display: block; }}
.brand-name {{ font-size: 14px; font-weight: 600; letter-spacing: 0.02em; }}
.brand-version {{ font-size: 12px; color: var(--ink3); font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
/* theme switch: a 3-way Auto/Light/Dark segmented pill, not a single
   on-off switch -- see the body:has() rules above for why a binary
   checkbox can't offer a real "back to light" path when the system
   default is already dark. */
.sw {{ display: flex; gap: 2px; background: var(--track); border-radius: 999px; padding: 2px; }}
.sw input {{ position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }}
.sw label {{
  cursor: pointer; font-size: 12px; color: var(--ink3); padding: 4px 10px; border-radius: 999px; user-select: none;
}}
.sw input#theme-auto:checked ~ label[for="theme-auto"],
.sw input#theme-light:checked ~ label[for="theme-light"],
.sw input#theme-dark:checked ~ label[for="theme-dark"] {{ background: var(--panel); color: var(--ink); box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
main {{ padding-top: 32px; padding-bottom: 56px; }}
section {{ padding: 44px 0; border-top: 1px solid var(--line); }}
section:first-child {{ padding-top: 0; border-top: none; }}
.eyebrow {{
  margin: 0 0 16px; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11.5px;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink3);
}}
h1 {{ margin: 0; font-size: clamp(30px, 5.4vw, 48px); line-height: 1.08; font-weight: 400; letter-spacing: -0.02em; max-width: 18ch; }}
.subtitle {{ margin: 18px 0 0; max-width: 60ch; font-size: 15.5px; color: var(--ink2); }}
h2 {{ margin: 0 0 4px; font-size: 22px; font-weight: 400; letter-spacing: -0.01em; }}
h2 .info {{ margin-left: 4px; }}
.section-sub {{ margin: 0 0 26px; font-size: 13px; color: var(--ink3); }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1px; background: var(--line); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
.stat {{ background: var(--bg); padding: 24px 22px; }}
.stat-label {{ margin: 0 0 10px; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink3); }}
.stat-value {{ margin: 0; font-size: clamp(26px, 4vw, 36px); line-height: 1; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
.stat-sub {{ margin: 10px 0 0; font-size: 12.5px; color: var(--ink2); }}
.stat-bar {{ margin: 14px 0 0; height: 3px; background: var(--track); border-radius: 2px; overflow: hidden; }}
.stat-bar > span {{ display: block; height: 100%; background: var(--ink); }}
.coverage {{ font-size: 13.5px; color: var(--ink2); }}
.coverage p {{ margin: 0 0 8px; }}
.coverage p:last-child {{ margin-bottom: 0; }}
.coverage code {{ color: var(--ink); }}
/* spend rows */
.row {{
  display: grid; grid-template-columns: minmax(140px, 1.1fr) minmax(0, 3fr) 88px 18px; align-items: center; gap: 18px;
  padding: 15px 0; border-top: 1px solid var(--hair); text-decoration: none; color: inherit;
}}
.row:last-child {{ border-bottom: 1px solid var(--hair); }}
.row-name {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
.dot {{ width: 7px; height: 7px; border-radius: 50%; flex: none; }}
/* Wrap rather than truncate -- the label is the data, not decoration.
   Ellipsis-truncating a bucket name is exactly the kind of information
   loss this whole report exists to avoid. */
.rowname {{ font-size: 14.5px; overflow-wrap: break-word; }}
.row-count {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11.5px; color: var(--ink3); flex: none; }}
.row-track {{ display: block; height: 20px; background: var(--track); border-radius: 3px; overflow: hidden; }}
.bar {{ display: block; height: 100%; }}
.row-value {{ text-align: right; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; font-variant-numeric: tabular-nums; }}
.rowgo {{ color: var(--ink3); font-size: 13px; text-align: right; }}
/* Below ~560px the 4-column grid doesn't have room for a full label next
   to a bar next to a dollar figure without truncating one of them --
   stack name+value on top, the bar spanning full width beneath, instead. */
@media (max-width: 560px) {{
  .row {{ grid-template-columns: 1fr auto; grid-template-areas: "name value" "bar bar"; row-gap: 10px; }}
  .row-name {{ grid-area: name; }}
  .row-value {{ grid-area: value; }}
  .row-track {{ grid-area: bar; }}
  .rowgo {{ display: none; }}
}}
/* buckets */
details.bucket {{ padding: 20px 0; border-top: 1px solid var(--line); }}
details.bucket summary {{ list-style: none; cursor: pointer; }}
details.bucket summary::-webkit-details-marker {{ display: none; }}
.bucket-head {{ display: flex; align-items: center; gap: 11px; flex-wrap: wrap; }}
.chev {{ display: inline-block; width: 12px; font-size: 10px; color: var(--ink3); transition: transform .25s ease; }}
details.bucket[open] .chev {{ transform: rotate(90deg); }}
.bucket-head h3 {{ margin: 0; font-size: 17px; font-weight: 500; letter-spacing: -0.005em; }}
.bucket-meta {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; color: var(--ink3); }}
.bucket-body {{ padding: 16px 0 0 23px; }}
.rule-block {{ max-width: 66ch; border-left: 1px solid var(--line); padding-left: 15px; margin-bottom: 22px; }}
.rule {{ margin: 0; font-size: 13.5px; color: var(--ink2); }}
.action {{ margin: 8px 0 0; font-size: 13.5px; color: var(--ink); }}
.action-label {{
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11px; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--ink3); margin-right: 6px;
}}
/* tabs */
.tabs {{ position: relative; }}
.tabs input {{ position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }}
.tabstrip {{ display: flex; gap: 20px; margin-bottom: 10px; }}
.tablabel {{ cursor: pointer; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink3); padding: 0 0 8px; border-bottom: 1.5px solid transparent; user-select: none; }}
.tabs input[data-tab="model"]:checked ~ .tabstrip label[for$="-model"],
.tabs input[data-tab="workflow"]:checked ~ .tabstrip label[for$="-workflow"] {{ color: var(--ink); border-bottom-color: var(--ink); }}
.panel {{ display: none; }}
.tabs input[data-tab="model"]:checked ~ .panel[data-tab="model"],
.tabs input[data-tab="workflow"]:checked ~ .panel[data-tab="workflow"] {{ display: block; }}
.table-scroll {{ overflow-x: auto; }}
table.breakdown {{ border-collapse: collapse; width: 100%; min-width: 420px; font-size: 13px; font-variant-numeric: tabular-nums; }}
table.breakdown caption {{ display: none; }}
table.breakdown th, table.breakdown td {{ padding: 10px 12px 10px 0; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }}
table.breakdown th {{ font-weight: 400; color: var(--ink3); }}
table.breakdown td {{ border-bottom: 1px solid var(--hair); }}
table.breakdown .rowkey {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12.5px; }}
table.breakdown td.num, table.breakdown th.num {{ text-align: right; padding-right: 4px; }}
tbody tr:hover {{ background: var(--sel); }}
/* sample cases */
details.cases {{ margin-top: 20px; font-size: 13px; color: var(--ink2); }}
details.cases summary {{ list-style: none; cursor: pointer; display: flex; align-items: center; gap: 9px; }}
details.cases summary::-webkit-details-marker {{ display: none; }}
.chev2 {{ display: inline-block; width: 10px; font-size: 9px; transition: transform .25s ease; }}
details.cases[open] .chev2 {{ transform: rotate(90deg); }}
.caselist {{ margin: 12px 0 0; padding: 0; list-style: none; display: grid; gap: 8px; }}
.case {{
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; line-height: 1.55;
  background: var(--panel); border: 1px solid var(--hair); border-radius: 6px; padding: 10px 12px;
}}
/* footer */
.page-footer {{
  margin-top: 8px; padding: 32px 0 48px; border-top: 1px solid var(--line); display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 26px; align-items: start;
}}
.page-footer p {{ margin: 0 0 8px; font-size: 13px; color: var(--ink2); }}
.page-footer .tagline strong {{ color: var(--ink); }}
.page-footer .reassurance {{ color: var(--ink3); }}
.footer-links {{ display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }}
.footer-links a {{ font-size: 13px; }}
a.quiet {{
  text-decoration: none; color: inherit; background-image: linear-gradient(var(--ink3), var(--ink3));
  background-size: 100% 1px; background-repeat: no-repeat; background-position: 0 100%;
}}
.footer-meta .meta {{ color: var(--ink3); font-size: 11.5px; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; margin-bottom: 0; }}
@media print {{
  .noprint {{ display: none !important; }}
  details > *:not(summary) {{ display: block !important; }}
  summary::-webkit-details-marker, summary::marker {{ display: none; }}
  .bucket, .cases {{ break-inside: avoid; }}
  a.quiet {{ background: none; }}
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ transition-duration: .001ms !important; }}
  html {{ scroll-behavior: auto; }}
}}
</style>
</head>
<body>
{header}
<main>

<section>
  <p class="eyebrow">{eyebrow}</p>
  <h1 class="serif">{headline}</h1>
  <p class="subtitle">Every pair below is a call your agent made more than once on the same execution path.</p>
</section>

<section>
  <div class="stat-grid">{stats}</div>
</section>

<section class="coverage">{coverage}</section>

<section>
  <h2 class="serif">Where the spend went</h2>
  <p class="section-sub">Cost by verdict, across the priced subset. Select a row to open its detail.</p>
  {rows}
</section>

<section>
  <h2 class="serif">The verdicts</h2>
  {sections}
  <p class="footnote coverage">{footnote}</p>
</section>

{unpriced_section}

</main>
{footer}
</body>
</html>
"""

_UNPRICED_SECTION_TEMPLATE = """
<section>
  <h2 class="serif">Events with no cost</h2>
  <p class="section-sub">The blind spot in every figure above.</p>
  <div class="stat-grid" style="margin-bottom:26px;">
    <div class="stat"><p class="stat-label">Events total</p><p class="stat-value serif">{total}</p></div>
    <div class="stat"><p class="stat-label">Priced</p><p class="stat-value serif">{priced}</p></div>
    <div class="stat"><p class="stat-label">No <code>cost_usd</code></p><p class="stat-value serif">{unpriced}</p></div>
  </div>
  <div class="rule-block">
    <p class="rule">{unpriced} event(s) carried no price, so no dollar figure in this report includes them. A repeat among them is invisible, not free.</p>
    <p class="action"><span class="action-label">Action</span> Set <code>cost_usd</code> on every span. Coverage under 80% makes totals indicative, not auditable.</p>
  </div>
</section>"""


def _stat_cell(label: str, value: str, sub: str, *, bar_pct: float | None = None) -> str:
    """`sub` is taken as already-safe HTML, not escaped here -- same
    contract as `value`. A caller composing `sub` from more than one part
    (see the "Pairs evaluated" cell below) must escape each part itself
    before joining; escaping the joined string here would double-escape
    any literal entity already in it (e.g. "&middot;" -> "&amp;middot;",
    which renders as the literal text "&middot;" instead of a middle dot --
    a bug that shipped once already for exactly this reason).
    """
    bar = f'<div class="stat-bar"><span style="width:{bar_pct:.0f}%"></span></div>' if bar_pct is not None else ""
    return (
        f'<div class="stat"><p class="stat-label">{html.escape(label)}</p>'
        f'<p class="stat-value serif">{value}</p>{bar}'
        f'<p class="stat-sub">{sub}</p></div>'
    )


def to_html(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render a self-contained HTML page: no CDN assets, no webfonts, no JS.
    Safe to open directly from disk or attach anywhere -- every value pulled
    from the trace is HTML-escaped before being interpolated. The header,
    theme toggle, per-bucket tabs, and collapsible sections are all CSS-only
    interactivity; there is no <script> tag anywhere in the output.
    """
    eyebrow, headline = _headline(result)
    coverage_html = _coverage_html(result.coverage)
    rows = _spend_rows(result.buckets)
    sections = "".join(
        _bucket_section(result, b, index=i, max_reasons=max_reasons)
        for i, b in enumerate(result.buckets)
    )

    top = result.buckets[0] if result.buckets else None
    bucket_parts = " &middot; ".join(
        f"{b.slice.count} {html.escape(b.label.lower())}" for b in result.buckets if b.slice.count
    ) or "nothing classified"
    stats = "".join([
        _stat_cell(
            top.label if top else "Top bucket",
            html.escape(_fmt_usd(top.slice.cost_usd)) if top and top.slice.cost_usd > 0
            else (str(top.slice.count) if top else "0"),
            html.escape(f"{top.slice.count} pair(s)" if top else "no candidate pairs"),
        ) if top else "",
        _stat_cell(
            "Pairs evaluated", str(result.total_candidates), bucket_parts,
        ),
        _stat_cell(
            "Trace coverage", f"{result.coverage.pricing_coverage_fraction * 100:.0f}%",
            html.escape(
                f"{result.coverage.priced_events} of {result.coverage.total_events} "
                "events carried a price"
            ),
            bar_pct=result.coverage.pricing_coverage_fraction * 100,
        ),
    ])

    unpriced_section = ""
    if result.coverage.unpriced_events:
        unpriced_section = _UNPRICED_SECTION_TEMPLATE.format(
            total=result.coverage.total_events,
            priced=result.coverage.priced_events,
            unpriced=result.coverage.unpriced_events,
        )

    return _HTML_TEMPLATE.format(
        header=_header_html(),
        footer=_footer_html(),
        eyebrow=eyebrow,
        headline=headline,
        stats=stats,
        coverage=coverage_html,
        rows=rows,
        sections=sections,
        footnote=html.escape(result.footnote or ""),
        unpriced_section=unpriced_section,
    )
