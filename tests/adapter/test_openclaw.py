import json
from pathlib import Path

from helpers import span, traces_document
from redundo.adapter.sources.openclaw import convert_openclaw

# gen_ai.input.messages/output.messages shape verified against
# @openclaw/diagnostics-otel's own test suite (service.test.ts), not
# invented -- see docs/openclaw.md.
INPUT_MESSAGES = json.dumps([
    {"role": "user", "parts": [{"type": "text", "content": "what changed?"}]},
])
OUTPUT_MESSAGES = json.dumps([
    {"role": "assistant", "parts": [{"type": "text", "content": "the trace changed"}],
     "finish_reason": "stop"},
])


def convert(document_or_documents):
    docs = (
        document_or_documents
        if isinstance(document_or_documents, list)
        else [document_or_documents]
    )
    return convert_openclaw(docs)


# --- span-name mapping ----------------------------------------------------

def test_model_call_span_produces_one_llm_call():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "gpt-5.4"})]
    records, summary = convert(traces_document(spans))
    assert len(records) == 1
    assert records[0]["event_type"] == "llm_call"
    assert records[0]["model"] == "gpt-5.4"
    assert summary.kept_spans == 1


def test_tool_execution_span_produces_one_tool_call():
    spans = [span("s1", trace_id="trace-1", name="openclaw.tool.execution", start=0, end=1,
                  attributes={"gen_ai.tool.name": "lookup"})]
    records, summary = convert(traces_document(spans))
    assert len(records) == 1
    assert records[0]["event_type"] == "tool_call"
    assert records[0]["name"] == "lookup"


def test_harness_run_and_run_spans_are_skipped_not_converted():
    spans = [
        span("h1", trace_id="trace-1", name="openclaw.harness.run", start=0, end=5),
        span("r1", trace_id="trace-1", parent_span_id="h1", name="openclaw.run", start=0, end=5),
        span("s1", trace_id="trace-1", parent_span_id="r1", name="openclaw.model.call",
             start=1, end=2, attributes={"gen_ai.request.model": "m"}),
    ]
    records, summary = convert(traces_document(spans))
    assert len(records) == 1
    assert summary.skipped_by_kind == {"openclaw.harness.run": 1, "openclaw.run": 1}


def test_model_usage_span_is_skipped_not_double_counted():
    # Distinct span from openclaw.model.call in the exporter source;
    # deliberately not treated as a second llm_call -- see module docstring
    # point 3.
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.usage", start=0, end=1)]
    records, summary = convert(traces_document(spans))
    assert records == []
    assert summary.skipped_by_kind == {"openclaw.model.usage": 1}


# --- task_id: always the trace ID, never a session id ---------------------

def test_task_id_is_always_the_trace_id():
    spans = [span("s1", trace_id="exact-trace-id", name="openclaw.model.call", start=0, end=1)]
    records, _ = convert(traces_document(spans))
    assert records[0]["task_id"] == "exact-trace-id"
    assert records[0]["metadata"]["task_id_source"] == "trace_id_fallback"


def test_session_scoped_attributes_have_no_effect_even_if_present():
    # OpenClaw's own exporter strips these before export, but if a
    # malformed/foreign capture somehow carried one, this adapter must
    # still never read it as task_id -- trace_id is the only signal it
    # trusts for this source.
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"openclaw.sessionKey": "session-key", "openclaw.sessionId": "sess-1"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["task_id"] == "trace-1"


# --- content: opaque without captureContent, real with it -----------------

def test_no_capture_content_degrades_to_opaque():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m"})]
    records, summary = convert(traces_document(spans))
    assert records[0]["metadata"]["content_basis"] == "opaque"
    assert summary.records_with_opaque_content == 1
    assert summary.records_with_prompt_content == 0


def test_capture_content_input_messages_hashed_as_real_content():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m", "gen_ai.input.messages": INPUT_MESSAGES})]
    records, summary = convert(traces_document(spans))
    assert records[0]["metadata"]["content_basis"] == "prompt"
    assert summary.records_with_prompt_content == 1


def test_capture_content_output_messages_becomes_response_hash():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={
                      "gen_ai.request.model": "m",
                      "gen_ai.input.messages": INPUT_MESSAGES,
                      "gen_ai.output.messages": OUTPUT_MESSAGES,
                  })]
    records, _ = convert(traces_document(spans))
    assert "response_hash" in records[0]["metadata"]


def test_two_identical_input_messages_hash_the_same():
    spans = [
        span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
             attributes={"gen_ai.request.model": "m", "gen_ai.input.messages": INPUT_MESSAGES}),
        span("s2", trace_id="trace-1", parent_span_id="s1", name="openclaw.model.call",
             start=2, end=3,
             attributes={"gen_ai.request.model": "m", "gen_ai.input.messages": INPUT_MESSAGES}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["content_hash"] == records[1]["content_hash"]


def test_two_opaque_records_never_coincidentally_match():
    spans = [
        span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
             attributes={"gen_ai.request.model": "m"}),
        span("s2", trace_id="trace-1", parent_span_id="s1", name="openclaw.model.call",
             start=2, end=3, attributes={"gen_ai.request.model": "m"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["content_hash"] != records[1]["content_hash"]


# --- tool_result: only emitted with real captured output ------------------

def test_tool_call_without_result_attribute_has_no_tool_result_event():
    spans = [span("s1", trace_id="trace-1", name="openclaw.tool.execution", start=0, end=1,
                  attributes={"gen_ai.tool.name": "lookup"})]
    records, _ = convert(traces_document(spans))
    assert len(records) == 1
    assert records[0]["event_type"] == "tool_call"


def test_tool_call_with_captured_result_produces_paired_tool_result():
    spans = [span("s1", trace_id="trace-1", name="openclaw.tool.execution", start=0, end=1,
                  attributes={
                      "gen_ai.tool.name": "lookup",
                      "gen_ai.tool.call.arguments": '{"q": "trace"}',
                      "gen_ai.tool.call.result": '{"rows": 1}',
                  })]
    records, _ = convert(traces_document(spans))
    assert [r["event_type"] for r in records] == ["tool_call", "tool_result"]
    assert records[1]["parent_id"] == records[0]["step_index"]


# --- cost_usd: always None for this source ---------------------------------

def test_cost_usd_is_always_none_even_if_captured():
    # Cost only ever appears on the metrics OTLP signal for this source,
    # never as a span attribute -- if a span somehow carried one anyway,
    # this adapter still must not surface it as ground truth. See module
    # docstring point 2.
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m", "openclaw.cost.usd": 0.05})]
    records, _ = convert(traces_document(spans))
    assert records[0]["cost_usd"] is None


# --- tokens -----------------------------------------------------------------

def test_tokens_in_sums_input_and_cache_tokens():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={
                      "gen_ai.request.model": "m",
                      "gen_ai.usage.input_tokens": 100,
                      "gen_ai.usage.cache_read.input_tokens": 30,
                      "gen_ai.usage.cache_creation.input_tokens": 5,
                      "gen_ai.usage.output_tokens": 20,
                  })]
    records, _ = convert(traces_document(spans))
    assert records[0]["tokens_in"] == 135
    assert records[0]["tokens_out"] == 20


# --- outcome -----------------------------------------------------------------

def test_error_category_marks_outcome_error():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m", "openclaw.errorCategory": "timeout"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["outcome"] == "error"


def test_blocked_tool_call_marks_outcome_error():
    spans = [span("s1", trace_id="trace-1", name="openclaw.tool.execution", start=0, end=1,
                  attributes={"gen_ai.tool.name": "exec", "openclaw.outcome": "blocked"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["outcome"] == "error"


def test_completed_span_with_no_error_is_ok():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["outcome"] == "ok"


# --- lineage / workflow ------------------------------------------------------

def test_workflow_comes_from_nearest_harness_run_ancestor():
    spans = [
        span("h1", trace_id="trace-1", name="openclaw.harness.run", start=0, end=5,
             attributes={"openclaw.harness.id": "claude-cli"}),
        span("s1", trace_id="trace-1", parent_span_id="h1", name="openclaw.model.call",
             start=1, end=2, attributes={"gen_ai.request.model": "m"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "claude-cli"


def test_workflow_falls_back_to_channel_with_no_harness_ancestor():
    spans = [span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
                  attributes={"gen_ai.request.model": "m", "openclaw.channel": "webchat"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "webchat"


def test_real_parent_span_id_becomes_lineage():
    spans = [
        span("s1", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
             attributes={"gen_ai.request.model": "m"}),
        span("s2", trace_id="trace-1", parent_span_id="s1", name="openclaw.model.call",
             start=2, end=3, attributes={"gen_ai.request.model": "m"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[1]["parent_id"] == records[0]["step_index"]


def test_sibling_tool_calls_under_a_flat_wrapper_chain_sequentially_by_interval():
    # Simulates an unverified-but-plausible flat topology: two tool calls
    # both direct children of the same wrapper span (never parented to
    # each other), anchored under a real kept ancestor further up -- the
    # scenario chain_tail exists for. A real kept ancestor above the flat
    # siblings is required for this to kick in at all: with no kept
    # ancestor anywhere above a flat group, this mechanism intentionally
    # leaves them unlinked rather than guessing which of possibly several
    # unrelated top-level flame graphs in the same trace they belong to
    # (see test_no_kept_ancestor_above_a_flat_group_stays_unlinked).
    spans = [
        span("anchor", trace_id="trace-1", name="openclaw.model.call", start=0, end=1,
             attributes={"gen_ai.request.model": "m"}),
        span("w1", trace_id="trace-1", parent_span_id="anchor", name="openclaw.run",
             start=1, end=10),
        span("s1", trace_id="trace-1", parent_span_id="w1", name="openclaw.tool.execution",
             start=2, end=3, attributes={"gen_ai.tool.name": "t"}),
        span("s2", trace_id="trace-1", parent_span_id="w1", name="openclaw.tool.execution",
             start=4, end=5, attributes={"gen_ai.tool.name": "t"}),
    ]
    records, _ = convert(traces_document(spans))
    tool_calls = [r for r in records if r["event_type"] == "tool_call"]
    assert tool_calls[0]["parent_id"] == records[0]["step_index"]  # anchor
    assert tool_calls[1]["parent_id"] == tool_calls[0]["step_index"]  # chained to sibling


def test_no_kept_ancestor_above_a_flat_group_stays_unlinked():
    # Two model calls flat under a top-level wrapper with nothing kept
    # above it -- the honest, conservative default: leave them unlinked
    # rather than risk chaining two genuinely independent top-level runs
    # in the same trace as if they were sequential.
    spans = [
        span("r1", trace_id="trace-1", name="openclaw.run", start=0, end=10),
        span("s1", trace_id="trace-1", parent_span_id="r1", name="openclaw.model.call",
             start=1, end=2, attributes={"gen_ai.request.model": "m"}),
        span("s2", trace_id="trace-1", parent_span_id="r1", name="openclaw.model.call",
             start=3, end=4, attributes={"gen_ai.request.model": "m"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["parent_id"] is None
    assert records[1]["parent_id"] is None


def test_overlapping_siblings_stay_independent_not_chained():
    # Genuine parallel fan-out (overlapping intervals) must never be
    # chained to each other -- both attach directly to the shared ancestor
    # (here: none, since there's no kept ancestor at all).
    spans = [
        span("r1", trace_id="trace-1", name="openclaw.run", start=0, end=10),
        span("s1", trace_id="trace-1", parent_span_id="r1", name="openclaw.model.call",
             start=1, end=5, attributes={"gen_ai.request.model": "m"}),
        span("s2", trace_id="trace-1", parent_span_id="r1", name="openclaw.model.call",
             start=2, end=6, attributes={"gen_ai.request.model": "m"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["parent_id"] is None
    assert records[1]["parent_id"] is None
