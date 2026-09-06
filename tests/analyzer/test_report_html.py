from redundo.analyzer.analyses import RULE_TEXT, WasteAnalysis
from redundo.analyzer.classify import Verdict
from redundo.analyzer.report import to_html, to_text
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                outcome=None, cost_usd=None, model=None, workflow=None, task_id="t1"):
    return Event(
        task_id=task_id, step_index=step_index, event_type=event_type, name=name,
        content_hash=content_hash, tokens_in=None, tokens_out=None, outcome=outcome,
        timestamp=None, cost_usd=cost_usd, model=model, parent_id=None, workflow=workflow,
        metadata={},
    )


def build(events):
    return WasteAnalysis().run(events)


def test_html_is_self_contained_no_external_resources():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    page = to_html(build(events))
    assert "<!doctype html>" in page.lower()
    assert "http://" not in page
    assert "https://" not in page
    assert "cdn" not in page.lower()
    assert "<script" not in page.lower()


def test_html_contains_all_three_bucket_labels():
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
    assert "0 candidate redundant-repeat pair" in page


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
    assert f"1 confirmed_waste -- {RULE_TEXT[Verdict.CONFIRMED_WASTE]}" in output


def test_html_shows_coverage_line():
    page = to_html(build(_confirmed_waste_events()))
    assert "Coverage" in page
    # 2 of 4 events are priced ($1.00 each) -- the reader needs this number
    # before trusting any dollar figure below it.
    assert "2/4 events priced" in page


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
