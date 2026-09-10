import json
from datetime import datetime, timedelta, timezone

from helpers import gauge_metric_document, span, traces_document
from redundo.adapter.sources.openclaw_localtrace import (
    OpenClawLocaltraceSource,
    convert_openclaw_localtrace,
)

INPUT_MESSAGES = json.dumps([{"prompt": "what changed?", "history": []}])
OUTPUT_MESSAGES = json.dumps(["the trace changed"])


def convert(document_or_documents):
    docs = (
        document_or_documents
        if isinstance(document_or_documents, list)
        else [document_or_documents]
    )
    return convert_openclaw_localtrace(docs)


def _turn(
    trace_id,
    *,
    session_id=None,
    model_start=1,
    model_end=2,
    tool=False,
    llm_call=False,
    channel=None,
    tokens_in=None,
    tokens_out=None,
    mutating=None,
    run_start=0,
    run_end=3,
    direct_cost_usd=None,
    pricing_generated_at=None,
):
    """One realistic run: run span + model.call span (+ optional
    tool.execution and llm.call siblings), matching the real span shapes
    openclaw-localtrace actually emits (confirmed live -- see
    docs/openclaw-localtrace.md).
    """
    run_attrs = {}
    if session_id:
        run_attrs["openclaw.sessionId"] = session_id
    if channel:
        run_attrs["openclaw.channel"] = channel
    spans = [
        span("run", trace_id=trace_id, name="openclaw-localtrace.run",
             start=run_start, end=run_end, attributes=run_attrs),
    ]
    model_attrs = {"openclaw.provider": "anthropic", "openclaw.model": "claude-sonnet-5"}
    if session_id:
        model_attrs["openclaw.sessionId"] = session_id
    spans.append(
        span("model", trace_id=trace_id, name="openclaw-localtrace.model.call",
             start=model_start, end=model_end, attributes=model_attrs)
    )
    if llm_call:
        llm_attrs = {}
        if tokens_in is not None:
            llm_attrs["gen_ai.usage.input_tokens"] = tokens_in
        if tokens_out is not None:
            llm_attrs["gen_ai.usage.output_tokens"] = tokens_out
        if direct_cost_usd is not None:
            llm_attrs["gen_ai.usage.cost_usd"] = direct_cost_usd
        if pricing_generated_at is not None:
            llm_attrs["openclaw.pricingTableGeneratedAt"] = pricing_generated_at
        llm_attrs["gen_ai.input.messages"] = INPUT_MESSAGES
        llm_attrs["gen_ai.output.messages"] = OUTPUT_MESSAGES
        spans.append(
            span("llm", trace_id=trace_id, name="openclaw-localtrace.llm.call",
                 start=model_start - 1, end=model_end + 1, attributes=llm_attrs)
        )
    if tool:
        tool_attrs = {
            "openclaw.toolName": "exec",
            "gen_ai.tool.call.arguments": json.dumps({"command": "echo hi"}),
            "gen_ai.tool.call.result": json.dumps({"content": "hi"}),
        }
        if mutating is not None:
            tool_attrs["openclaw.mutatingAction"] = mutating
        if session_id:
            tool_attrs["openclaw.sessionId"] = session_id
        spans.append(
            span("tool", trace_id=trace_id, name="openclaw-localtrace.tool.execution",
                 start=model_end, end=model_end + 1, attributes=tool_attrs)
        )
    return spans


# --- span-name mapping ------------------------------------------------------

def test_model_call_span_produces_one_llm_call():
    records, summary = convert(traces_document(_turn("t1")))
    llm_calls = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_calls) == 1
    assert llm_calls[0]["model"] == "claude-sonnet-5"
    assert summary.kept_spans == 1


def test_run_span_is_skipped_not_converted():
    records, summary = convert(traces_document(_turn("t1")))
    assert summary.skipped_by_kind == {"openclaw-localtrace.run": 1}
    assert all(r["event_type"] != "run" for r in records)


def test_tool_execution_span_produces_call_and_result():
    records, _ = convert(traces_document(_turn("t1", tool=True)))
    calls = [r for r in records if r["event_type"] == "tool_call"]
    results = [r for r in records if r["event_type"] == "tool_result"]
    assert len(calls) == 1
    assert len(results) == 1
    assert calls[0]["name"] == "exec"
    assert results[0]["parent_id"] is None  # flat topology -- see module docstring point 4


def test_an_unmapped_span_name_is_counted_but_not_converted():
    spans = _turn("t1") + [span("x", trace_id="t1", name="openclaw-localtrace.session.stuck", start=5)]
    records, summary = convert(traces_document(spans))
    assert summary.skipped_by_kind["openclaw-localtrace.session.stuck"] == 1
    assert len(records) == 1  # only the model.call


# --- task_id: real session id when captureIdentifiers is on ----------------

def test_task_id_is_real_session_id_when_present():
    records, summary = convert(traces_document(_turn("t1", session_id="sess-1")))
    assert records[0]["task_id"] == "sess-1"
    assert records[0]["metadata"]["task_id_source"] == "conversation_id"
    assert summary.sessions_found == 1


def test_task_id_falls_back_to_trace_id_when_no_session_id():
    records, summary = convert(traces_document(_turn("exact-trace-id")))
    assert records[0]["task_id"] == "exact-trace-id"
    assert records[0]["metadata"]["task_id_source"] == "trace_id_fallback"
    assert summary.sessions_found == 0


def test_two_runs_sharing_a_session_id_merge_into_one_task_with_continuous_steps():
    # Different trace_ids (different turns), same real session -- this is
    # the capability the plugin unlocks that sources.openclaw never had.
    spans_a = _turn("trace-a", session_id="sess-1", model_start=1, model_end=2)
    spans_b = _turn("trace-b", session_id="sess-1", model_start=10, model_end=11)
    records, _ = convert(traces_document(spans_a + spans_b))
    llm_calls = sorted(
        (r for r in records if r["event_type"] == "llm_call"), key=lambda r: r["step_index"]
    )
    assert len(llm_calls) == 2
    assert {r["task_id"] for r in llm_calls} == {"sess-1"}
    assert [r["step_index"] for r in llm_calls] == [0, 1]


def test_two_runs_with_different_session_ids_stay_separate_tasks():
    spans_a = _turn("trace-a", session_id="sess-1")
    spans_b = _turn("trace-b", session_id="sess-2")
    records, _ = convert(traces_document(spans_a + spans_b))
    task_ids = {r["task_id"] for r in records if r["event_type"] == "llm_call"}
    assert task_ids == {"sess-1", "sess-2"}


# --- llm.call pairing: the real merge, not an enrichment --------------------

def test_llm_call_span_content_and_tokens_are_merged_into_the_llm_call_record():
    records, summary = convert(
        traces_document(_turn("t1", llm_call=True, tokens_in=10, tokens_out=20))
    )
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["metadata"]["content_basis"] == "prompt"
    assert record["tokens_in"] == 10
    assert record["tokens_out"] == 20
    assert "response_hash" in record["metadata"]
    assert summary.llm_call_spans_paired == 1
    assert summary.records_with_prompt_content == 1


def test_model_call_with_no_llm_call_sibling_degrades_to_opaque_content():
    records, summary = convert(traces_document(_turn("t1", llm_call=False)))
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["metadata"]["content_basis"] == "opaque"
    assert record["tokens_in"] is None
    assert summary.records_with_opaque_content == 1


def test_one_llm_call_span_pairs_with_the_last_of_several_model_call_spans_it_contains():
    # Regression test for a real bug found against real production data: a
    # 13-iteration tool loop inside one run produced exactly one llm.call
    # span, bracketing all 13 model.call spans -- not a 1:1 companion the
    # way an earlier, smaller-scale test wrongly assumed. The old
    # count-mismatch guard discarded content/tokens for the ENTIRE run
    # whenever this happened -- see module docstring point 2.
    spans = _turn("t1", model_start=1, model_end=2)
    spans.append(
        span("model2", trace_id="t1", name="openclaw-localtrace.model.call",
             start=5, end=6, attributes={"openclaw.model": "m2"})
    )
    spans.append(
        span("llm", trace_id="t1", name="openclaw-localtrace.llm.call", start=0, end=8,
             attributes={"gen_ai.input.messages": INPUT_MESSAGES, "gen_ai.output.messages": OUTPUT_MESSAGES})
    )
    records, summary = convert(traces_document(spans))
    llm_calls = sorted((r for r in records if r["event_type"] == "llm_call"), key=lambda r: r["step_index"])
    assert len(llm_calls) == 2
    assert llm_calls[0]["metadata"]["content_basis"] == "opaque"  # earlier call in the run
    assert llm_calls[1]["metadata"]["content_basis"] == "prompt"  # the LAST call gets the content
    assert llm_calls[1]["model"] == "m2"
    assert summary.llm_call_spans_paired == 1
    assert summary.llm_call_spans_unpaired == 0


def test_an_llm_call_span_containing_no_model_call_span_at_all_is_left_unpaired():
    spans = [
        span("run", trace_id="t1", name="openclaw-localtrace.run", start=0, end=5),
        span("llm", trace_id="t1", name="openclaw-localtrace.llm.call", start=0, end=1,
             attributes={"gen_ai.input.messages": INPUT_MESSAGES}),
        span("model", trace_id="t1", name="openclaw-localtrace.model.call", start=2, end=3,
             attributes={"openclaw.model": "m"}),
    ]
    records, summary = convert(traces_document(spans))
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["metadata"]["content_basis"] == "opaque"
    assert summary.llm_call_spans_unpaired == 1
    assert summary.llm_call_spans_paired == 0


# --- write signal: the capability unlock ------------------------------------

def test_mutating_action_true_becomes_write_true_on_the_tool_call():
    records, _ = convert(traces_document(_turn("t1", tool=True, mutating=True)))
    call = next(r for r in records if r["event_type"] == "tool_call")
    assert call["metadata"]["write"] is True


def test_mutating_action_false_becomes_write_false_on_the_tool_call():
    records, _ = convert(traces_document(_turn("t1", tool=True, mutating=False)))
    call = next(r for r in records if r["event_type"] == "tool_call")
    assert call["metadata"]["write"] is False


def test_write_flag_never_appears_on_the_tool_result():
    records, _ = convert(traces_document(_turn("t1", tool=True, mutating=True)))
    result = next(r for r in records if r["event_type"] == "tool_result")
    assert "write" not in result["metadata"]


def test_no_mutating_action_attribute_leaves_write_unset():
    records, _ = convert(traces_document(_turn("t1", tool=True)))
    call = next(r for r in records if r["event_type"] == "tool_call")
    assert "write" not in call["metadata"]


# --- workflow ----------------------------------------------------------------

def test_workflow_comes_from_the_run_spans_channel_attribute():
    records, _ = convert(traces_document(_turn("t1", channel="discord")))
    assert records[0]["workflow"] == "discord"


def test_workflow_is_none_without_a_channel():
    records, _ = convert(traces_document(_turn("t1")))
    assert records[0]["workflow"] is None


# --- cost apportionment ------------------------------------------------------

def test_direct_gen_ai_cost_usd_attribute_becomes_cost_usd_on_the_record():
    records, summary = convert(traces_document(_turn("t1", llm_call=True, direct_cost_usd=0.0123)))
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["cost_usd"] == 0.0123
    assert record["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"
    assert summary.llm_calls_with_direct_cost == 1


# --- pricing-table age: this plugin's own, not OpenClaw's built-in one -----

def test_pricing_table_generated_at_is_copied_into_metadata():
    generated_at = "2026-01-01T00:00:00.000Z"
    records, summary = convert(traces_document(
        _turn("t1", llm_call=True, direct_cost_usd=0.05, pricing_generated_at=generated_at)
    ))
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["metadata"]["pricing_table_generated_at"] == generated_at
    assert summary.pricing_table_generated_at == generated_at


def test_notes_always_show_the_pricing_table_age_not_just_when_stale():
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    _, summary = convert(traces_document(
        _turn("t1", llm_call=True, direct_cost_usd=0.05, pricing_generated_at=recent)
    ))
    notes_text = " ".join(summary.notes())
    assert "generated" in notes_text
    assert "not OpenClaw's built-in pricing" in notes_text
    assert "over 30 days" not in notes_text  # recent -- no staleness escalation


def test_notes_escalate_past_the_30_day_staleness_threshold():
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    _, summary = convert(traces_document(
        _turn("t1", llm_call=True, direct_cost_usd=0.05, pricing_generated_at=old)
    ))
    notes_text = " ".join(summary.notes())
    assert "over 30 days" in notes_text
    assert "npx openclaw-localtrace-update-pricing" in notes_text


def test_no_direct_cost_records_means_no_pricing_age_note_at_all():
    _, summary = convert(traces_document(_turn("t1", llm_call=False)))
    assert summary.pricing_table_generated_at is None
    notes_text = " ".join(summary.notes())
    assert "pricing table" not in notes_text.lower() or "generated" not in notes_text


def test_summary_tracks_the_latest_generated_at_across_multiple_direct_cost_records():
    older = "2026-01-01T00:00:00.000Z"
    newer = "2026-06-01T00:00:00.000Z"
    spans_a = _turn("trace-a", session_id="sess-1", llm_call=True, direct_cost_usd=0.01,
                     pricing_generated_at=newer, model_start=1, model_end=2, run_start=0, run_end=3)
    spans_b = _turn("trace-b", session_id="sess-1", llm_call=True, direct_cost_usd=0.02,
                     pricing_generated_at=older, model_start=10, model_end=11, run_start=9, run_end=12)
    _, summary = convert(traces_document(spans_a + spans_b))
    assert summary.pricing_table_generated_at == newer


def test_direct_cost_takes_priority_over_turn_level_apportionment():
    # Regression test: a real capture where the plugin's own per-call
    # estimate and the turn-cost gauge would otherwise both try to price
    # the same record -- the direct estimate must win, not get overwritten.
    spans = _turn("t1", session_id="sess-1", llm_call=True, direct_cost_usd=0.05,
                  run_start=0, run_end=3)
    trace_doc = traces_document(spans)
    metrics_doc = gauge_metric_document(
        [{"value": 0.99, "time": 5, "attributes": {"openclaw.sessionId": "sess-1"}}],
        name="openclaw.turn.cost.usd",
    )
    records, summary = convert([trace_doc, metrics_doc])
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["cost_usd"] == 0.05
    assert record["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"
    assert summary.llm_calls_with_direct_cost == 1
    assert summary.llm_calls_with_apportioned_cost == 0


def test_turn_level_apportionment_still_fills_in_calls_with_no_direct_estimate():
    # One call gets a direct per-call estimate, the other doesn't (e.g.
    # an unrecognized model) -- the turn-cost point should apportion only
    # across the one still missing a price, not re-split across both.
    spans = _turn("t1", session_id="sess-1", model_start=1, model_end=2,
                  llm_call=True, direct_cost_usd=0.05, run_start=0, run_end=6)
    spans.append(
        span("model2", trace_id="t1", name="openclaw-localtrace.model.call", start=3, end=4,
             attributes={"openclaw.model": "m2", "openclaw.sessionId": "sess-1"})
    )
    trace_doc = traces_document(spans)
    metrics_doc = gauge_metric_document(
        [{"value": 0.20, "time": 10, "attributes": {"openclaw.sessionId": "sess-1"}}],
        name="openclaw.turn.cost.usd",
    )
    records, summary = convert([trace_doc, metrics_doc])
    llm_calls = sorted((r for r in records if r["event_type"] == "llm_call"), key=lambda r: r["step_index"])
    assert len(llm_calls) == 2
    assert llm_calls[0]["cost_usd"] == 0.05  # the direct estimate, untouched
    assert llm_calls[1]["cost_usd"] == 0.20  # the whole apportioned point, not half of it
    assert summary.llm_calls_with_direct_cost == 1
    assert summary.llm_calls_with_apportioned_cost == 1


def test_turn_cost_gauge_point_is_apportioned_to_that_runs_llm_call():
    spans = _turn("t1", session_id="sess-1", llm_call=True, tokens_in=10, tokens_out=0,
                  run_start=0, run_end=3)
    trace_doc = traces_document(spans)
    metrics_doc = gauge_metric_document(
        [{"value": 0.5, "time": 5, "attributes": {"openclaw.sessionId": "sess-1"}}],
        name="openclaw.turn.cost.usd",
    )
    records, summary = convert([trace_doc, metrics_doc])
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["cost_usd"] == 0.5
    assert record["metadata"]["cost_basis"] == "apportioned_from_metrics_by_tokens"
    assert summary.llm_calls_with_apportioned_cost == 1


def test_cost_split_evenly_across_two_llm_calls_with_no_token_data():
    spans = _turn("t1", session_id="sess-1", model_start=1, model_end=2, run_start=0, run_end=5)
    spans.append(
        span("model2", trace_id="t1", name="openclaw-localtrace.model.call", start=3, end=4,
             attributes={"openclaw.model": "m2", "openclaw.sessionId": "sess-1"})
    )
    trace_doc = traces_document(spans)
    metrics_doc = gauge_metric_document(
        [{"value": 1.0, "time": 10, "attributes": {"openclaw.sessionId": "sess-1"}}],
        name="openclaw.turn.cost.usd",
    )
    records, summary = convert([trace_doc, metrics_doc])
    llm_calls = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_calls) == 2
    assert all(r["cost_usd"] == 0.5 for r in llm_calls)
    assert all(r["metadata"]["cost_basis"] == "apportioned_from_metrics_equal_split" for r in llm_calls)
    assert summary.llm_calls_apportioned_by_equal_split == 2


def test_cost_point_with_no_session_attribute_is_not_apportioned():
    spans = _turn("t1", session_id="sess-1")
    trace_doc = traces_document(spans)
    metrics_doc = gauge_metric_document(
        [{"value": 0.5, "time": 5, "attributes": {}}], name="openclaw.turn.cost.usd"
    )
    records, summary = convert([trace_doc, metrics_doc])
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["cost_usd"] is None
    assert summary.cost_points_found == 1
    assert summary.llm_calls_with_apportioned_cost == 0


def test_no_metrics_documents_means_no_cost_data_at_all():
    records, summary = convert(traces_document(_turn("t1", session_id="sess-1")))
    record = next(r for r in records if r["event_type"] == "llm_call")
    assert record["cost_usd"] is None
    assert summary.cost_points_found == 0


def test_two_turns_in_the_same_session_each_get_their_own_cost_point():
    spans_a = _turn("trace-a", session_id="sess-1", model_start=1, model_end=2,
                     run_start=0, run_end=3, tokens_in=None)
    spans_b = _turn("trace-b", session_id="sess-1", model_start=20, model_end=21,
                     run_start=19, run_end=22, tokens_in=None)
    trace_doc = traces_document(spans_a + spans_b)
    metrics_doc = gauge_metric_document(
        [
            {"value": 0.10, "time": 5, "attributes": {"openclaw.sessionId": "sess-1"}},
            {"value": 0.40, "time": 25, "attributes": {"openclaw.sessionId": "sess-1"}},
        ],
        name="openclaw.turn.cost.usd",
    )
    records, _ = convert([trace_doc, metrics_doc])
    llm_calls = sorted(
        (r for r in records if r["event_type"] == "llm_call"), key=lambda r: r["step_index"]
    )
    assert len(llm_calls) == 2
    assert llm_calls[0]["cost_usd"] == 0.10
    assert llm_calls[1]["cost_usd"] == 0.40


# --- detect() ------------------------------------------------------------------

def test_detect_by_span_name_prefix():
    doc = traces_document([span("s1", name="openclaw-localtrace.model.call", start=0)])
    detection = OpenClawLocaltraceSource().detect([doc])
    assert detection is not None
    assert detection.source == "openclaw-localtrace"
    assert "span name" in detection.reason


def test_detect_by_resource_service_name():
    doc = traces_document(
        [span("s1", name="some.unrelated.span", start=0)],
        resource_attributes={"service.name": "openclaw-localtrace"},
    )
    detection = OpenClawLocaltraceSource().detect([doc])
    assert detection is not None
    assert "service.name" in detection.reason


def test_detect_does_not_false_positive_on_the_old_openclaw_source():
    # The hyphen is load-bearing: "openclaw-localtrace." must never be
    # mistaken for -- or mistake -- "openclaw."'s own span family.
    doc = traces_document([span("s1", name="openclaw.model.call", start=0)])
    assert OpenClawLocaltraceSource().detect([doc]) is None


def test_detect_returns_none_for_unrelated_data():
    doc = traces_document([span("s1", name="something.else", start=0)])
    assert OpenClawLocaltraceSource().detect([doc]) is None


def test_real_registry_discovers_this_source():
    from redundo.adapter.registry import SourceRegistry

    registry = SourceRegistry(discover=True)
    assert "openclaw-localtrace" in registry.names()
