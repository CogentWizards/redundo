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
    assert "Trace coverage" in page
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
    assert output.count("step=2 (tool_call/search)") == 2


def test_synthesized_cost_only_events_get_their_own_section_at_the_end():
    events = [
        make_event(0, event_type="llm_call", cost_usd=0.5,
                    metadata={"synthesized_cost_only": True}),
    ]
    result = build(events)
    text = to_text(result)
    assert text.rstrip().endswith(
        "None of them can appear in any bucket, they have no content and "
        "no repeat to classify."
    )
    assert "Spend outside the trace structure:" in text

    page = to_html(result)
    assert "Spend outside the trace structure" in page
    assert page.index("Spend outside the trace structure") > page.index("The verdicts")


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
    assert "at 1,000 calls/day: ~$15,000.00/mo projected" in text
    assert "cost_usd:   1.000000  (this sample only)" in text


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
