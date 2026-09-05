import json
from pathlib import Path

from redundo.adapter.sources.openinference import convert_openinference
from helpers import span, traces_document

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_document():
    return json.loads((FIXTURES / "sample_otlp.json").read_text(encoding="utf-8"))


def convert(document_or_documents):
    """Accept either a single document (most tests here build one) or a
    list, mirroring how real captures usually span many small batch files.
    """
    docs = (
        document_or_documents
        if isinstance(document_or_documents, list)
        else [document_or_documents]
    )
    return convert_openinference(docs)


# --- task_id resolution -----------------------------------------------

def test_conversation_id_used_when_present_on_any_span():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "gen_ai.conversation.id": "conv-1"}),
    ]
    records, summary = convert(traces_document(spans))
    assert records[0]["task_id"] == "conv-1"
    assert summary.traces_with_conversation_id == 1
    assert summary.traces_fallback_to_trace_id == 0


def test_falls_back_to_trace_id_when_conversation_id_absent():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, summary = convert(traces_document(spans))
    assert records[0]["task_id"] == "trace-1"
    assert summary.traces_fallback_to_trace_id == 1
    assert any("cross-trace rework not detected" in n for n in summary.notes())


def test_conflicting_conversation_id_falls_back_and_is_flagged():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "gen_ai.conversation.id": "conv-a"}),
        span("s2", trace_id="trace-1", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "bye",
                         "gen_ai.conversation.id": "conv-b"}),
    ]
    records, summary = convert(traces_document(spans))
    assert all(r["task_id"] == "trace-1" for r in records)
    assert summary.traces_ambiguous_conversation_id == 1
    assert summary.traces_fallback_to_trace_id == 1
    assert any("conflicting" in n for n in summary.notes())


def test_never_synthesizes_a_task_id():
    # No conversation.id anywhere; task_id must be exactly the trace ID,
    # never anything derived/hashed/invented.
    spans = [
        span("s1", trace_id="exact-trace-id-value", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["task_id"] == "exact-trace-id-value"


# --- span kind mapping ---------------------------------------------------

def test_llm_kind_produces_one_record():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi", "llm.model_name": "gpt-5.6",
    })]
    records, summary = convert(traces_document(spans))
    assert len(records) == 1
    assert records[0]["event_type"] == "llm_call"
    assert records[0]["model"] == "gpt-5.6"
    assert summary.kept_spans == 1


def test_tool_kind_produces_call_and_result():
    spans = [span("s1", start=0, status_code=1, attributes={
        "openinference.span.kind": "TOOL", "tool.name": "search",
        "input.value": "{}", "output.value": "{\"ok\": true}",
    })]
    records, _ = convert(traces_document(spans))
    assert len(records) == 2
    assert records[0]["event_type"] == "tool_call"
    assert records[1]["event_type"] == "tool_result"
    assert records[1]["outcome"] == "ok"
    assert records[1]["parent_id"] == records[0]["step_index"]


def test_tool_kind_error_status_maps_to_error_outcome():
    spans = [span("s1", start=0, status_code=2, attributes={
        "openinference.span.kind": "TOOL", "tool.name": "search",
        "input.value": "{}", "output.value": "{}",
    })]
    records, _ = convert(traces_document(spans))
    assert records[1]["outcome"] == "error"


def test_tool_with_no_output_value_still_produces_call_only():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "TOOL", "tool.name": "search", "input.value": "{}",
    })]
    records, _ = convert(traces_document(spans))
    assert len(records) == 1
    assert records[0]["event_type"] == "tool_call"


def test_unsupported_kind_is_skipped_and_counted():
    spans = [
        span("s1", start=0, attributes={"openinference.span.kind": "RETRIEVER", "input.value": "q"}),
    ]
    records, summary = convert(traces_document(spans))
    assert records == []
    assert summary.skipped_by_kind == {"RETRIEVER": 1}
    assert any("RETRIEVER" in n for n in summary.notes())


def test_missing_kind_is_skipped_and_counted_as_missing():
    spans = [span("s1", start=0, attributes={"input.value": "q"})]
    records, summary = convert(traces_document(spans))
    assert records == []
    assert summary.skipped_by_kind == {"(missing)": 1}


def test_missing_input_value_drops_the_span():
    spans = [span("s1", start=0, attributes={"openinference.span.kind": "LLM"})]
    records, summary = convert(traces_document(spans))
    assert records == []
    assert summary.skipped_missing_content == 1


# --- lineage / parent_id --------------------------------------------------

def test_parent_id_walks_through_skipped_ancestors():
    # agent (skipped, root) -> llm1 (kept: no kept ancestor -> parent_id None)
    #                        -> chain (skipped, child of llm1)
    #                             -> llm2 (kept: real OTLP parent is the
    #                                      skipped CHAIN, but the walk must
    #                                      land on llm1's step instead of
    #                                      giving up at the first skip)
    spans = [
        span("agent", start=0, attributes={"openinference.span.kind": "AGENT"}),
        span("llm1", parent_span_id="agent", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "first"}),
        span("chain", parent_span_id="llm1", start=2, attributes={"openinference.span.kind": "CHAIN"}),
        span("llm2", parent_span_id="chain", start=3,
             attributes={"openinference.span.kind": "LLM", "input.value": "second"}),
    ]
    records, _ = convert(traces_document(spans))
    llm1_record, llm2_record = records
    assert llm1_record["parent_id"] is None  # no kept ancestor exists at all
    assert llm2_record["parent_id"] == llm1_record["step_index"]


def test_tool_result_parent_id_is_its_own_call():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "TOOL", "input.value": "{}", "output.value": "{}",
    })]
    records, _ = convert(traces_document(spans))
    call, result = records
    assert result["parent_id"] == call["step_index"]


def test_step_index_is_sequential_and_time_ordered():
    spans = [
        span("s2", start=10, attributes={"openinference.span.kind": "LLM", "input.value": "b"}),
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "a"}),
    ]
    records, _ = convert(traces_document(spans))
    assert [r["step_index"] for r in records] == [0, 1]
    assert records[0]["content_hash"] != records[1]["content_hash"]
    # s1 starts earlier than s2 despite appearing second in the input list;
    # step 0 must belong to s1.
    assert records[0]["metadata"]["otlp_span_id"] == "s1"


# --- workflow best-effort --------------------------------------------------

def test_workflow_is_nearest_agent_or_chain_ancestor_name():
    spans = [
        span("agent", start=0, name="research_agent", attributes={"openinference.span.kind": "AGENT"}),
        span("llm1", parent_span_id="agent", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "research_agent"


def test_workflow_is_none_with_no_agent_or_chain_ancestor():
    spans = [span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] is None


# --- masking diagnostics ---------------------------------------------------

def test_masked_spans_recorded_in_metadata_and_summary():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM",
        "input.value": "call at 2026-08-31T12:00:00Z",
    })]
    records, summary = convert(traces_document(spans))
    assert records[0]["metadata"]["masked_spans"] == 1
    assert summary.records_with_any_mask == 1
    assert summary.masked_span_total == 1


def test_hash_spec_present_on_every_record():
    from redundo.adapter.hashing import HASH_SPEC
    spans = [span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["metadata"]["hash_spec"] == HASH_SPEC


# --- end-to-end against the realistic fixture ------------------------------

def test_fixture_document_produces_a_detectable_repeat():
    records, summary = convert(load_fixture_document())
    tool_calls = [r for r in records if r["event_type"] == "tool_call"]
    assert len(tool_calls) == 2
    # differently key-ordered but semantically identical JSON args must hash the same
    assert tool_calls[0]["content_hash"] == tool_calls[1]["content_hash"]
    assert summary.traces_with_conversation_id == 1
    assert summary.skipped_by_kind == {"AGENT": 1, "CHAIN": 1}


def test_task_id_source_recorded_as_conversation_id():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.conversation.id": "conv-1",
    })]
    records, _ = convert(traces_document(spans))
    assert records[0]["metadata"]["task_id_source"] == "conversation_id"


def test_task_id_source_recorded_as_trace_id_fallback():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
    })]
    records, _ = convert(traces_document(spans))
    assert records[0]["metadata"]["task_id_source"] == "trace_id_fallback"
