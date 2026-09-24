from redundo.analyzer.analyses import RULE_TEXT, WasteAnalysis
from redundo.analyzer.classify import Verdict
from redundo.analyzer.report import _palette_for, to_html, to_text
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                outcome=None, cost_usd=None, model=None, workflow=None, task_id="t1",
                metadata=None):
    return Event(
        task_id=task_id, step_index=step_index, event_type=event_type, name=name,
        content_hash=content_hash, tokens_in=None, tokens_out=None, outcome=outcome,
        timestamp=None, cost_usd=cost_usd, model=model, parent_id=None, workflow=workflow,
        metadata=metadata or {},
    )


def build(events):
    return WasteAnalysis().run(events)


def test_html_is_self_contained_no_external_resources():
    # "Self-contained" means no network request happens on open: no fetched
    # image/script/stylesheet. It does not mean no hyperlink at all -- the
    # footer links to GitHub, which is just a clickable <a>, not a resource
    # the page loads. Distinguish the two explicitly rather than asserting
    # no "https://" appears anywhere, which would also reject those links.
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    page = to_html(build(events))
    assert "<!doctype html>" in page.lower()
    assert "cdn" not in page.lower()
    assert "<script" not in page.lower()
    assert 'src="http' not in page  # no fetched image/script
    assert "stylesheet" not in page.lower()  # no external CSS
    assert "<img" not in page.lower()  # the header logo is CSS, not a fetched or embedded image
    assert 'class="brand-mark"' in page


def test_html_contains_all_four_bucket_labels():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    page = to_html(build(events))
    assert "Confirmed waste" in page
    assert "Likely legitimate" in page
    assert "Unclassified" in page
    assert "Near duplicate" in page


def test_html_shows_sample_cases_for_the_near_duplicate_bucket_too():
    # Sample cases aren't special-cased per bucket in report.py -- every
    # bucket's own result.reasons get the same collapsible treatment. This
    # pins that near_duplicate specifically (not just the three exact-match
    # verdicts) actually gets it, since it's easy to eyeball a demo trace
    # with zero near-duplicates and assume the section is missing rather
    # than just empty.
    zero_fp = "0" * 16
    close_fp = "f" * 2 + "0" * 14  # Hamming distance 8, within default threshold
    events = [
        make_event(0, task_id="t2", content_hash="a", cost_usd=0.03, model="gpt-5.6",
                   metadata={"similarity_fingerprint": zero_fp}),
        make_event(1, task_id="t2", content_hash="b", cost_usd=0.04, model="gpt-5.6",
                   metadata={"similarity_fingerprint": close_fp}),
    ]
    page = to_html(build(events))
    assert "Sample cases to spot-check by hand (1)" in page
    assert "Hamming distance 8/64 bits" in page


def test_fourth_bucket_gets_its_own_palette_color_not_a_wraparound():
    # WasteAnalysis now emits 4 buckets; _PALETTE previously had exactly 3
    # entries, so index 3 wrapped to index 0's colors (identical to
    # confirmed_waste, indistinguishable in the rendered report). Guards
    # against that regression directly, independent of any one analysis's
    # bucket count.
    assert _palette_for(3) != _palette_for(0)


def test_all_six_waste_analysis_buckets_get_distinct_colors():
    # The real regression: _PALETTE had 4 colors for 6 buckets, so
    # position 4 (cross_task_redundancy) wrapped to position 0's red
    # (confirmed_waste's "waste verdict" color) and position 5
    # (recurring_pattern) wrapped to position 1's green (likely_
    # legitimate's "legitimate verdict" color) -- both buckets whose own
    # rule_text says "not a waste or legitimate verdict."
    colors = [_palette_for(i) for i in range(6)]
    assert len(set(colors)) == 6


def test_html_escapes_untrusted_trace_content():
    payload = '<script>alert(1)</script>'
    events = [
        make_event(0, event_type="tool_call", model=payload, workflow=payload),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", model=payload, workflow=payload, outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    page = to_html(build(events))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_html_renders_with_no_candidate_pairs():
    page = to_html(build([make_event(0)]))
    assert "No repeated calls found." in page


def _confirmed_waste_events():
    return [
        make_event(0, event_type="tool_call", cost_usd=1.0),
        make_event(1, event_type="tool_result", content_hash="same", cost_usd=None),
        make_event(2, event_type="tool_call", cost_usd=1.0, outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error", cost_usd=None),
    ]


def test_html_shows_rule_text_next_to_each_bucket_not_just_the_count():
    import html as html_module

    page = to_html(build(_confirmed_waste_events()))
    # The count alone ("1") is not checkable; the rule text is what makes
    # a reader able to verify a bucket by hand instead of trusting a label.
    # html.escape()'d, same as the page itself does to this same string.
    assert html_module.escape(RULE_TEXT[Verdict.CONFIRMED_WASTE]) in page
    assert html_module.escape(RULE_TEXT[Verdict.LIKELY_LEGITIMATE]) in page
    assert html_module.escape(RULE_TEXT[Verdict.UNCLASSIFIED]) in page


def test_text_shows_rule_text_next_to_the_count():
    output = to_text(build(_confirmed_waste_events()))
    assert f"1 confirmed_waste: {RULE_TEXT[Verdict.CONFIRMED_WASTE]}" in output


def test_html_shows_coverage_line():
    page = to_html(build(_confirmed_waste_events()))
    # 2 of 4 events are priced ($1.00 each) -- the reader needs this number
    # before trusting any dollar figure below it.
    assert "2 of 4 events carried a price" in page
    # A bare "$" is ambiguous outside the US; this report never converts
    # currency (see report.py's own module docstring for why), so it says
    # explicitly, once, that every figure is USD instead of leaving it implicit.
    assert "All amounts are USD" in page


def test_text_shows_coverage_line():
    output = to_text(build(_confirmed_waste_events()))
    assert output.startswith("Coverage:")
    assert "2/4 events priced" in output


def test_coverage_notes_unpriced_events_are_excluded_from_dollar_totals():
    output = to_text(build(_confirmed_waste_events()))
    assert "excluded from every dollar figure" in output


def test_text_shows_comparability_line_when_a_task_has_no_candidate_pairs():
    # A single, non-repeated event -- one task, zero candidate pairs.
    output = to_text(build([make_event(0)]))
    assert "0/1 tasks" in output
    assert "nothing that repeated at all" in output


def test_html_shows_comparability_line_when_a_task_has_no_candidate_pairs():
    page = to_html(build([make_event(0)]))
    assert "0/1 tasks" in page
    assert "nothing that repeated at all" in page


def test_comparability_line_omitted_when_every_task_has_a_candidate_pair():
    # _confirmed_waste_events() is a single task with one candidate pair --
    # every task is covered, so this line has nothing to add.
    output = to_text(build(_confirmed_waste_events()))
    assert "nothing that repeated at all" not in output
    page = to_html(build(_confirmed_waste_events()))
    assert "nothing that repeated at all" not in page


def test_max_reasons_truncates_at_render_time():
    # Truncation moved from analysis time (keep_reasons in WasteAnalysis's
    # constructor) to render time (max_reasons on the renderer) so a
    # renderer can show fewer than the analysis kept without re-running it.
    events = []
    for i in range(6):
        task_id = f"t{i}"
        events.append(make_event(0, event_type="tool_call", cost_usd=1.0, task_id=task_id))
        events.append(make_event(1, event_type="tool_result", content_hash="same",
                                   task_id=task_id))
        events.append(make_event(2, event_type="tool_call", cost_usd=1.0, outcome="error",
                                   task_id=task_id))
        events.append(make_event(3, event_type="tool_result", content_hash="same",
                                   outcome="error", task_id=task_id))
    result = build(events)
    assert len(result.reasons["confirmed_waste"]) == 6  # WasteAnalysis kept all 6
    output = to_text(result, max_reasons=2)
    # The "    - " prefix is only ever used by the sample-cases list, never
    # by "Fix these first" (a "N. $cost for ..." prefix instead), so this
    # isolates reasons-list truncation from the unrelated highlights
    # section, which also mentions "step=2 (tool_call/search)" for these
    # same priced events but is never capped by max_reasons.
    assert output.count("    - task=") == 2
    assert output.count("step=2 (tool_call/search)") > 2


def test_synthesized_cost_only_events_get_their_own_section_at_the_end():
    events = [
        make_event(0, event_type="llm_call", cost_usd=0.5,
                    metadata={"synthesized_cost_only": True}),
    ]
    result = build(events)
    text = to_text(result)
    assert (
        "None of them can appear in any bucket, they have no content and "
        "no repeat to classify."
    ) in text
    assert "Spend outside the trace structure:" in text
    # Spot-checkable by hand, not just a count and a dollar figure.
    assert text.rstrip().endswith("task=A step=0 (llm_call/search): billing-only, no matching span")

    page = to_html(result)
    assert "Spend outside the trace structure" in page
    assert page.index("Spend outside the trace structure") > page.index("Exact repeats")
    assert "Sample billing-only records to spot-check by hand" in page


def test_no_synthesized_cost_only_section_when_there_are_none():
    events = [make_event(0, event_type="llm_call", cost_usd=0.5)]
    result = build(events)
    assert "Spend outside the trace structure" not in to_text(result)
    assert "Spend outside the trace structure" not in to_html(result)


# --- cost basis breakdown ---------------------------------------------------

def test_cost_basis_line_shown_when_estimated_and_direct_are_mixed():
    events = [
        make_event(0, event_type="llm_call", cost_usd=1.0),
        make_event(1, event_type="llm_call", cost_usd=1.0, content_hash="b",
                    metadata={"cost_basis": "estimated_from_bundled_pricing_table"}),
    ]
    result = build(events)
    text = to_text(result)
    assert "Cost basis: $1.00 (50%) reported directly by the source, " \
        "$1.00 (50%) estimated from a bundled pricing table." in text
    page = to_html(result)
    assert "Cost basis:" in page
    assert "estimated from a bundled pricing table" in page


def test_cost_basis_line_absent_when_nothing_priced():
    events = [make_event(0, event_type="tool_call")]
    result = build(events)
    assert "Cost basis:" not in to_text(result)
    assert "Cost basis:" not in to_html(result)


# --- monthly cost projection -------------------------------------------------

_CONFIRMED_WASTE_EVENTS = [
    make_event(0, event_type="tool_call", cost_usd=1.0),
    make_event(1, event_type="tool_result", content_hash="same"),
    make_event(2, event_type="tool_call", cost_usd=1.0, outcome="error"),
    make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
]


def test_text_projection_defaults_to_1000_calls_per_day():
    # 2 tool_call events -> total_call_events=2; confirmed_waste's own
    # cost_usd is the repeat's 1.0 -> cost_per_call=0.5 -> $0.5*1000*30=$15000/mo.
    result = build(_CONFIRMED_WASTE_EVENTS)
    text = to_text(result)
    assert "Dollar figures below are also projected at 1,000 calls/day" in text
    assert "cost_usd:   1.000000" in text  # the real number leads, no caveat: fully priced
    assert "at 1,000 calls/day: ~$15,000.00/mo projected (hypothetical, see above)" in text


def test_text_projection_respects_calls_per_day_kwarg():
    result = build(_CONFIRMED_WASTE_EVENTS)
    text = to_text(result, calls_per_day=10)
    assert "at 10 calls/day: ~$150.00/mo projected" in text


def test_html_top_stat_card_shows_projection_not_the_raw_cent_figure():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "$15,000.00/mo" in page
    assert "$1.00 in this sample" in page


def test_html_bucket_meta_and_spend_row_show_both_figures():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "~$15,000.00/mo projected" in page
    assert "row-value-main" in page and "row-value-sample" in page


def test_json_includes_projection_and_per_bucket_fields():
    import json as _json

    from redundo.analyzer.report import to_json

    result = build(_CONFIRMED_WASTE_EVENTS)
    data = _json.loads(to_json(result, calls_per_day=10))
    assert data["projection"]["calls_per_day"] == 10
    assert data["projection"]["days_per_month"] == 30
    bucket = data["by_bucket"]["confirmed_waste"]
    assert bucket["cost_per_call_usd"] == 0.5
    assert bucket["projected_monthly_usd"] == 150.0


def test_no_projection_when_no_bucket_has_cost():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
    ]
    result = build(events)
    text = to_text(result)
    assert "projected" not in text
    assert "Dollar figures below are also projected" not in text
    page = to_html(result)
    assert '<p class="projection-caption">' not in page


# --- lead with count, demote dollars -----------------------------------------

def test_html_stat_grid_leads_with_pairs_evaluated_not_a_dollar_figure():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    pairs_idx = page.index("Pairs evaluated")
    top_bucket_idx = page.index("Confirmed waste")
    third_idx = page.index("Verdicts reached")  # confidence_stat, not Trace coverage
    assert pairs_idx < top_bucket_idx < third_idx


def test_html_top_bucket_stat_value_is_the_count_not_a_dollar_figure():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    # The top bucket's own stat-value cell holds the bare count ("2"),
    # never a dollar figure -- the dollar figure moved to stat-sub.
    stat_cell = page[page.index('<p class="stat-label">Confirmed waste'):]
    value_start = stat_cell.index('stat-value serif">') + len('stat-value serif">')
    value_end = stat_cell.index("</p>", value_start)
    assert stat_cell[value_start:value_end] == "1"


def test_html_bucket_meta_attaches_priced_fraction_when_partial():
    events = _CONFIRMED_WASTE_EVENTS + [
        make_event(4, event_type="tool_call", cost_usd=None, task_id="t2"),
        make_event(5, event_type="tool_result", content_hash="same", task_id="t2"),
        make_event(6, event_type="tool_call", cost_usd=None, outcome="error", task_id="t2"),
        make_event(7, event_type="tool_result", content_hash="same", outcome="error",
                    task_id="t2"),
    ]
    result = build(events)
    confirmed_waste = next(b for b in result.buckets if b.key == "confirmed_waste")
    assert confirmed_waste.slice.unpriced_count == 1
    page = to_html(result)
    assert "(1 of 2 repeat(s) priced)" not in page  # text-mode phrasing, not HTML's
    assert "(50% priced)" in page


def test_html_bucket_meta_has_no_caveat_when_fully_priced():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "% priced)" not in page


def test_text_priced_caveat_attached_to_cost_usd_line():
    events = _CONFIRMED_WASTE_EVENTS + [
        make_event(4, event_type="tool_call", cost_usd=None, task_id="t2"),
        make_event(5, event_type="tool_result", content_hash="same", task_id="t2"),
        make_event(6, event_type="tool_call", cost_usd=None, outcome="error", task_id="t2"),
        make_event(7, event_type="tool_result", content_hash="same", outcome="error",
                    task_id="t2"),
    ]
    result = build(events)
    text = to_text(result)
    assert "cost_usd:   1.000000  (1 of 2 repeat(s) priced)" in text


def test_spend_section_states_the_real_coverage_percentage():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "across the 50% of events (2 of 4) that carried a price" in page


# --- insight_text rendering ---------------------------------------------------

def test_html_renders_confirmed_waste_insight_text():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert 'class="insight"' in page
    assert "stuck, not working" in page


def test_text_renders_confirmed_waste_insight_text():
    result = build(_CONFIRMED_WASTE_EVENTS)
    text = to_text(result)
    assert "Your agent repeated itself, learned nothing new, and still failed" in text


# --- "Fix these first" -------------------------------------------------------

def test_html_fix_these_first_section_present_when_highlights_exist():
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "Fix these first" in page
    assert '<ul class="highlights">' in page
    assert "1. $1.00 for task=A" in page


def test_html_no_fix_these_first_section_when_no_highlights():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
    ]
    result = build(events)
    page = to_html(result)
    assert '<h2 class="serif">Fix these first</h2>' not in page


def test_text_fix_these_first_present_when_highlights_exist():
    result = build(_CONFIRMED_WASTE_EVENTS)
    text = to_text(result)
    assert "Fix these first:" in text
    assert "  1. $1.00 for task=A" in text


# --- confidence_stat replaces "Trace coverage" as the third stat cell -------

def test_html_confidence_stat_replaces_trace_coverage_cell():
    result = build(_CONFIRMED_WASTE_EVENTS)
    assert result.confidence_stat is not None
    page = to_html(result)
    assert "Verdicts reached" in page
    assert "Trace coverage" not in page


def test_html_falls_back_to_trace_coverage_when_no_confidence_stat():
    events = [
        make_event(0, content_hash="a", metadata={"similarity_fingerprint": "0" * 16}),
        make_event(1, content_hash="b", metadata={"similarity_fingerprint": "f" * 2 + "0" * 14}),
    ]
    result = build(events)
    assert result.confidence_stat is None
    page = to_html(result)
    assert "Trace coverage" in page


def test_text_confidence_stat_line_present():
    result = build(_CONFIRMED_WASTE_EVENTS)
    text = to_text(result)
    # n=1 is below the percent-display threshold: a bare fraction.
    assert "Verdicts reached: 1/1 (exact repeats judged" in text


def test_confidence_stat_shows_a_percentage_once_denominator_is_large_enough():
    events = []
    for i in range(5):
        task_id = f"t{i}"
        events.append(make_event(0, event_type="tool_call", task_id=task_id))
        events.append(make_event(1, event_type="tool_call", task_id=task_id))
    result = build(events)
    label, value, sub = result.confidence_stat
    assert "%" in value


# --- uninformative by-model/by-workflow breakdowns are suppressed -----------

def test_breakdown_still_shown_when_only_unknown_model_and_unlabeled_workflow():
    # _CONFIRMED_WASTE_EVENTS sets neither model nor workflow -- a
    # tool_call bucket legitimately has no model concept at all, and
    # that's real information (not a rendering bug to hide).
    result = build(_CONFIRMED_WASTE_EVENTS)
    page = to_html(result)
    assert "By model" in page
    assert "By workflow" in page
    assert "(unknown model)" in page
    assert "(unlabeled workflow)" in page


def test_breakdown_shown_when_a_real_model_is_present():
    events = [
        make_event(0, event_type="tool_call", cost_usd=1.0, model="gpt-5.6"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", cost_usd=1.0, outcome="error", model="gpt-5.6"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = build(events)
    page = to_html(result)
    assert "By model" in page
    assert "gpt-5.6" in page


# --- source_path is never rendered in a report ------------------------------
# coverage.source_path exists (see metrics.py) and is exposed in JSON, but
# a human-facing report never states a local filesystem path -- that's a
# fact about the machine that produced the report, not about the trace.

def test_source_path_never_rendered_even_when_present():
    events = [make_event(0, metadata={"source_path": "/tmp/otlp_traces"})]
    result = build(events)
    text = to_text(result)
    assert "/tmp/otlp_traces" not in text
    assert "Captured from" not in text
    page = to_html(result)
    assert "/tmp/otlp_traces" not in page
    assert "Captured from" not in page


# --- grouped sections (Bucket.group / AnalysisResult.group_descriptions) ---

def _mixed_group_events():
    zero_fp = "0" * 16
    close_fp = "f" * 2 + "0" * 14  # Hamming distance 8, within default threshold
    return [
        # confirmed_waste pair (task t1):
        make_event(0, event_type="tool_call", task_id="t1"),
        make_event(1, event_type="tool_result", content_hash="same", task_id="t1"),
        make_event(2, event_type="tool_call", outcome="error", task_id="t1"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error", task_id="t1"),
        # near_duplicate pair (task t2):
        make_event(0, task_id="t2", content_hash="a", metadata={"similarity_fingerprint": zero_fp}),
        make_event(1, task_id="t2", content_hash="b", metadata={"similarity_fingerprint": close_fp}),
    ]


def test_headline_splits_by_group_without_naming_confirmed_waste():
    from redundo.analyzer.report import _headline

    result = build(_mixed_group_events())
    _, headline = _headline(result)
    assert "exact repeats" in headline
    assert "similar or related" in headline
    assert "waste" not in headline.lower()


def test_html_renders_two_group_sections_not_one_flat_verdicts_section():
    page = to_html(build(_mixed_group_events()))
    assert '<h2 class="serif">Exact repeats</h2>' in page
    assert '<h2 class="serif">Similar or related</h2>' in page
    assert "The verdicts" not in page
    # Exact repeats comes first (confirmed_waste kept first in order).
    assert page.index("Exact repeats") < page.index("Similar or related")


def test_text_renders_two_group_headers():
    text = to_text(build(_mixed_group_events()))
    assert "Exact repeats:" in text
    assert "Similar or related:" in text
    assert text.index("Exact repeats:") < text.index("Similar or related:")


def test_ungrouped_analysis_still_renders_one_flat_verdicts_section():
    # A conforming third-party AnalysisResult that never sets Bucket.group
    # must render exactly like before this feature existed.
    from redundo.analyzer.analysis import AnalysisResult, Bucket
    from redundo.analyzer.metrics import Slice, compute_generic_coverage

    result = AnalysisResult(
        coverage=compute_generic_coverage([]),
        buckets=[Bucket(key="a", label="A", rule_text="rule a", slice=Slice(count=1))],
        analysis_name="custom",
    )
    page = to_html(result)
    assert '<h2 class="serif">The verdicts</h2>' in page


# --- no bucket auto-opens; insight/action suppressed at zero ----------------

def test_no_bucket_is_open_by_default():
    page = to_html(build(_confirmed_waste_events()))
    assert "<details" in page
    assert " open>" not in page and "\" open>" not in page.replace('id="bucket-confirmed_waste"', "")


def test_empty_confirmed_waste_bucket_suppresses_insight_and_action():
    # A trace with a real near_duplicate pair but zero confirmed_waste.
    zero_fp = "0" * 16
    close_fp = "f" * 2 + "0" * 14
    events = [
        make_event(0, content_hash="a", metadata={"similarity_fingerprint": zero_fp}),
        make_event(1, content_hash="b", metadata={"similarity_fingerprint": close_fp}),
    ]
    result = build(events)
    assert next(b for b in result.buckets if b.key == "confirmed_waste").slice.count == 0
    page = to_html(result)
    assert "stuck, not working" not in page
    assert "Cache the result or guard the retry" not in page
    text = to_text(result)
    assert "stuck, not working" not in text


# --- bar suppression at small max_value --------------------------------------

def test_bars_suppressed_when_max_bucket_value_is_small():
    # _confirmed_waste_events() has exactly one candidate pair -> every
    # bucket's count is 0 or 1, max_value=1, which used to render every
    # nonzero bucket's bar at width:100%.
    page = to_html(build(_confirmed_waste_events()))
    assert "row-no-bars" in page
    assert '<span class="row-track">' not in page


def test_bars_shown_when_max_bucket_value_is_large_enough():
    events = []
    for i in range(4):
        events.append(make_event(0, event_type="tool_call", task_id=f"t{i}"))
        events.append(make_event(1, event_type="tool_result", content_hash="same", task_id=f"t{i}"))
        events.append(make_event(2, event_type="tool_call", outcome="error", task_id=f"t{i}"))
        events.append(make_event(3, event_type="tool_result", content_hash="same", outcome="error",
                                   task_id=f"t{i}"))
    page = to_html(build(events))
    assert '<span class="row-track">' in page


# --- "Where the spend went" retitled when nothing is priced ------------------

def test_spend_section_retitled_when_no_bucket_has_cost():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = build(events)
    assert not any(b.slice.cost_usd > 0 for b in result.buckets)
    page = to_html(result)
    assert "Where the repeats landed" in page
    assert "Where the spend went" not in page
    # The bucket-meta lines (the six-times-over "$0.0000" the coverage
    # regression was about) are suppressed; the coverage paragraph's own
    # real $0.0000 total (an accurate fact: nothing was priced) is fine.
    assert '<span class="bucket-meta">0 pair(s)</span>' in page
    assert '<span class="bucket-meta">1 pair(s)</span>' in page


def test_spend_section_keeps_cost_framing_when_something_is_priced():
    page = to_html(build(_confirmed_waste_events()))
    assert "Where the spend went" in page


# --- task ID legend and shortening -------------------------------------------

def test_task_ids_shortened_and_legend_rendered():
    events = [
        make_event(0, event_type="tool_call", task_id="210f09cc-0969-4d3c-9626-03ae71dea57a"),
        make_event(1, event_type="tool_result", content_hash="same",
                    task_id="210f09cc-0969-4d3c-9626-03ae71dea57a"),
        make_event(2, event_type="tool_call", outcome="error",
                    task_id="210f09cc-0969-4d3c-9626-03ae71dea57a"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error",
                    task_id="210f09cc-0969-4d3c-9626-03ae71dea57a"),
    ]
    result = build(events)
    page = to_html(result)
    assert "210f09cc-0969-4d3c-9626-03ae71dea57a" not in page.split("Task ID legend")[0]
    assert "task=A" in page
    assert "Task ID legend" in page
    assert "task A = 210f09cc-0969-4d3c-9626-03ae71dea57a" in page

    text = to_text(result)
    assert "task=A" in text
    assert "Task legend:" in text
    assert "task A = 210f09cc-0969-4d3c-9626-03ae71dea57a" in text


def test_two_distinct_tasks_get_two_distinct_labels_in_first_seen_order():
    events = [
        make_event(0, event_type="tool_call", task_id="task-zzz"),
        make_event(1, event_type="tool_result", content_hash="same", task_id="task-zzz"),
        make_event(2, event_type="tool_call", outcome="error", task_id="task-zzz"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error", task_id="task-zzz"),
        make_event(0, event_type="tool_call", task_id="task-aaa"),
        make_event(1, event_type="tool_result", content_hash="same2", task_id="task-aaa"),
        make_event(2, event_type="tool_call", outcome="error", task_id="task-aaa"),
        make_event(3, event_type="tool_result", content_hash="same2", outcome="error", task_id="task-aaa"),
    ]
    result = build(events)
    text = to_text(result)
    # task-zzz's own candidate pair is classified first (reasons/highlights
    # are built in classification order), so it gets label A.
    assert "task A = task-zzz" in text
    assert "task B = task-aaa" in text


def test_no_legend_when_no_task_ids_appear_in_rendered_text():
    # Priced and un-repeated: no unpriced_samples, no reasons, no
    # highlights -- nowhere for a task= reference to come from at all.
    result = build([make_event(0, cost_usd=1.0)])
    text = to_text(result)
    assert "Task legend:" not in text
    page = to_html(result)
    assert "Task ID legend" not in page


# --- "waste" no longer frames the whole document -----------------------------

def test_footer_and_email_never_say_waste():
    page = to_html(build(_confirmed_waste_events()))
    assert "waste report" not in page.lower()
    assert "waste-detection" not in page.lower()
    assert "what's wasted" not in page.lower()
