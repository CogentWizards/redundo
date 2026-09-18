"""Render an AnalysisResult as text, JSON, or a self-contained HTML page.

Generic over any analysis's output. This module doesn't know what a
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
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
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
        f"({pct:.0f}%). {_fmt_usd(coverage.total_priced_cost_usd)} of tracked spend "
        "is what this analysis actually covers."
    ]
    if coverage.unpriced_events:
        lines.append(
            f"  {coverage.unpriced_events} event(s) had no cost_usd and are excluded "
            "from every dollar figure below. Percentages are computed on the "
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
        lines.append(f"{s.count} {bucket.key}: {bucket.rule_text}")
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

    if result.coverage.synthesized_cost_only_events:
        lines.append("")
        lines.append("Spend outside the trace structure:")
        lines.append(
            f"  {result.coverage.synthesized_cost_only_events} event(s) "
            f"({_fmt_usd(result.coverage.synthesized_cost_only_usd)}) have real "
            "cost_usd but no matching span at all: billing-only records, "
            "synthesized so this spend isn't silently missing from the totals "
            "above. None of them can appear in any bucket, they have no content "
            "and no repeat to classify."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
#
# Self-contained: no CDN, no webfonts, no JS framework. This has to run on
# someone's own trace data, possibly offline, and possibly years from now.
# A network dependency is a bug waiting to happen. The serif display font
# and oklch color tokens below are all system/fallback stacks and inline
# CSS values, never fetched. The header mark is four CSS-colored spans,
# not an image, so there's nothing to fetch or embed for it either.
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
# All dollar figures in this report are USD, always. That's a property
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

# (accent, accent-fill-light, accent-fill-dark): oklch, cycled by bucket
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


def _source_checkout_version(start: Path | None = None) -> str | None:
    """If this file is running from a source checkout (an editable
    install, `uv run` inside the repo, etc.), read the version straight
    out of its pyproject.toml instead of trusting installed package
    metadata. Package metadata is only refreshed on the next
    reinstall/sync, so it can drift from a locally-edited version field
    (confirmed: `uv run --no-sync` after bumping pyproject.toml's version
    by hand still reports the old one via importlib.metadata). Plain
    `uv run` auto-syncs and doesn't hit this, but a pip editable install,
    a frozen/offline environment, or any workflow that skips the sync
    step can. Returns None (fall back to installed metadata) when no
    pyproject.toml is found nearby, or when one is found but isn't
    redundo's own. For example, this package installed as a dependency inside
    some other project's tree, where an ancestor directory's
    pyproject.toml belongs to that other project, not this one.

    No TOML parser: adapt/analyze proper have zero dependencies (see
    README), a real constraint this shouldn't spend on a cosmetic version
    string. This only ever needs one well-known field out of a file this
    project fully controls the shape of.

    `start` defaults to this file's own location; a test passes a fake
    path to exercise the lookup without needing a real second checkout.
    """
    here = (start or Path(__file__)).resolve()
    for parent in list(here.parents)[:8]:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text()
        except OSError:
            return None
        name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
        if not name_match or name_match.group(1) != "redundo":
            return None  # a real pyproject.toml, just not this project's
        version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        return version_match.group(1) if version_match else None
    return None


def _redundo_version() -> str:
    return _source_checkout_version() or _installed_version()


def _installed_version() -> str:
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
    how many landed in the analysis's own most prominent bucket (position 0
    in result.buckets: the analysis orders its own buckets, this renderer
    just trusts that order, same as everywhere else it uses bucket
    position). Returns (eyebrow, headline).

    The eyebrow is always the generic "Event analysis", not
    result.analysis_name ("waste"): the bucket labels and the headline
    itself already say what kind of finding this is, so repeating that in
    the eyebrow too just adds noise above the actual sentence.
    """
    eyebrow = "Event analysis"
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
        f"({pct:.0f}%). <strong>{html.escape(_fmt_usd(coverage.total_priced_cost_usd))}</strong> "
        "of tracked spend is what this analysis actually covers. "
        "<strong>All amounts are USD</strong>, as reported by each event's own "
        "<code>cost_usd</code>; this report never converts or estimates a currency.</p>"
    ]
    if coverage.unpriced_events:
        parts.append(
            f"<p>{coverage.unpriced_events} event(s) had no <code>cost_usd</code> and are "
            "excluded from every dollar figure above and below. Percentages are computed "
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
    proportional bar, and a dollar (or pair-count) figure. Replaces what
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


def _sample_cases_html(reasons: list[str], *, label: str = "Sample cases to spot-check by hand") -> str:
    """The collapsible "spot-check these by hand" list, shared between a
    bucket's own sample reasons and the unpriced-events samples in the
    "Events with no cost" section below. Empty input renders nothing.
    """
    if not reasons:
        return ""
    items = "".join(f'<li class="case">{html.escape(r)}</li>' for r in reasons)
    return f"""
<details class="cases">
  <summary><span class="chev2">&#9656;</span> {html.escape(label)} ({len(reasons)})</summary>
  <ul class="caselist">{items}</ul>
</details>"""


def _bucket_section(result: AnalysisResult, bucket: Bucket, *, index: int, max_reasons: int) -> str:
    by_model = result.by_bucket_and_model.get(bucket.key, {})
    by_workflow = result.by_bucket_and_workflow.get(bucket.key, {})
    reasons = result.reasons.get(bucket.key, [])[:max_reasons]
    accent, *_ = _palette_for(index)

    reasons_html = _sample_cases_html(reasons)

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
    version = html.escape(_redundo_version())
    return f"""
<header class="page-header">
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">
      <span></span><span></span><span></span><span></span>
    </span>
    <span class="brand-name">redundo</span>
    <span class="brand-version">v{version}</span>
  </div>
  <div class="sw noprint">
    <input type="checkbox" id="theme">
    <label for="theme" title="Toggle dark mode" aria-label="Toggle dark mode">
      <span class="knob">
        <svg class="ico ico-sun" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"></circle><path d="M12 1.8v2.4M12 19.8v2.4M1.8 12h2.4M19.8 12h2.4M4.8 4.8l1.7 1.7M17.5 17.5l1.7 1.7M19.2 4.8l-1.7 1.7M6.5 17.5l-1.7 1.7"></path></svg>
        <svg class="ico ico-moon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 14.6A8.8 8.8 0 019.4 3.5a8.8 8.8 0 1011.1 11.1z"></path></svg>
      </span>
    </label>
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
/* Deliberately no @media (prefers-color-scheme) block: the default is
   always light, the checkbox is the only way to get dark. A resting
   default that already follows the system preference plus a checkbox
   that can only add "force dark" has a real gap for a system-dark
   reader: dark on top of dark, with no path back to light. A single
   checkbox can only offer one real override direction, so it gets the
   one that's unambiguous: always-light by default, dark on demand. */
body:has(#theme:checked) {{
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
.brand-mark {{ display: grid; grid-template-columns: 6px 6px; grid-template-rows: 6px 6px; gap: 2px; }}
.brand-mark span:nth-child(1) {{ background: #3d73d9; border-radius: 1px; }}
.brand-mark span:nth-child(2) {{ background: #138e83; border-radius: 1px; }}
.brand-mark span:nth-child(3) {{ background: #3f9c62; border-radius: 1px; }}
.brand-mark span:nth-child(4) {{ background: #d87935; border-radius: 1px; }}
.brand-name {{ font-size: 14px; font-weight: 600; letter-spacing: 0.02em; }}
.brand-version {{ font-size: 12px; color: var(--ink3); font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
/* theme switch */
.sw input {{ position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }}
.sw label {{
  display: flex; align-items: center; width: 46px; height: 26px; padding: 3px; border-radius: 999px;
  background: var(--track); cursor: pointer; position: relative; box-shadow: inset 0 0 0 1px var(--hair);
}}
.knob {{
  position: absolute; left: 3px; top: 3px; width: 20px; height: 20px; border-radius: 50%; background: var(--panel);
  box-shadow: 0 1px 3px rgba(0,0,0,.18); display: grid; place-items: center; color: var(--ink2);
  transition: transform .3s cubic-bezier(.2,.8,.25,1);
}}
.sw input:checked + label .knob {{ transform: translateX(20px); }}
.ico {{ position: absolute; transition: opacity .2s ease, transform .3s cubic-bezier(.2,.8,.25,1); }}
.ico-moon {{ opacity: 0; transform: rotate(-45deg) scale(.6); }}
.sw input:checked + label .ico-sun {{ opacity: 0; transform: rotate(45deg) scale(.6); }}
.sw input:checked + label .ico-moon {{ opacity: 1; transform: none; }}
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
/* Wrap rather than truncate. The label is the data, not decoration, and
   ellipsis-truncating a bucket name is exactly the kind of information
   loss this whole report exists to avoid. */
.rowname {{ font-size: 14.5px; overflow-wrap: break-word; }}
.row-count {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11.5px; color: var(--ink3); flex: none; }}
.row-track {{ display: block; height: 20px; background: var(--track); border-radius: 3px; overflow: hidden; }}
.bar {{ display: block; height: 100%; }}
.row-value {{ text-align: right; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; font-variant-numeric: tabular-nums; }}
.rowgo {{ color: var(--ink3); font-size: 13px; text-align: right; }}
/* Below ~560px the 4-column grid doesn't have room for a full label next
   to a bar next to a dollar figure without truncating one of them, so
   stack name+value on top instead, with the bar spanning full width
   beneath. */
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

{untraceable_spend_section}

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
    {samples}
  </div>
</section>"""

_UNTRACEABLE_SPEND_SECTION_TEMPLATE = """
<section>
  <h2 class="serif">Spend outside the trace structure</h2>
  <p class="section-sub">Real cost with no matching span at all.</p>
  <div class="stat-grid" style="margin-bottom:26px;">
    <div class="stat"><p class="stat-label">Event(s)</p><p class="stat-value serif">{count}</p></div>
    <div class="stat"><p class="stat-label">Spend</p><p class="stat-value serif">{cost}</p></div>
  </div>
  <div class="rule-block">
    <p class="rule">A billing-only record: <code>cost_usd</code> is real, but there was no span to attach it to, so it was synthesized rather than silently dropped. Already included in the totals above. It can never appear in a bucket, it has no content and no repeat to classify.</p>
  </div>
</section>"""


def _stat_cell(label: str, value: str, sub: str, *, bar_pct: float | None = None) -> str:
    """`sub` is taken as already-safe HTML, not escaped here, same
    contract as `value`. A caller composing `sub` from more than one part
    (see the "Pairs evaluated" cell below) must escape each part itself
    before joining. Escaping the joined string here would double-escape
    any literal entity already in it (e.g. "&middot;" becomes
    "&amp;middot;", which renders as the literal text "&middot;" instead
    of a middle dot, a bug that shipped once already for exactly this
    reason).
    """
    bar = f'<div class="stat-bar"><span style="width:{bar_pct:.0f}%"></span></div>' if bar_pct is not None else ""
    return (
        f'<div class="stat"><p class="stat-label">{html.escape(label)}</p>'
        f'<p class="stat-value serif">{value}</p>{bar}'
        f'<p class="stat-sub">{sub}</p></div>'
    )


def to_html(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render a self-contained HTML page: no CDN assets, no webfonts, no JS.
    Safe to open directly from disk or attach anywhere. Every value pulled
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
        unpriced_samples_html = _sample_cases_html(
            result.coverage.unpriced_samples[:max_reasons],
            label="Sample unpriced events to spot-check by hand",
        )
        unpriced_section = _UNPRICED_SECTION_TEMPLATE.format(
            total=result.coverage.total_events,
            priced=result.coverage.priced_events,
            unpriced=result.coverage.unpriced_events,
            samples=unpriced_samples_html,
        )

    untraceable_spend_section = ""
    if result.coverage.synthesized_cost_only_events:
        untraceable_spend_section = _UNTRACEABLE_SPEND_SECTION_TEMPLATE.format(
            count=result.coverage.synthesized_cost_only_events,
            cost=html.escape(_fmt_usd(result.coverage.synthesized_cost_only_usd)),
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
        untraceable_spend_section=untraceable_spend_section,
    )
