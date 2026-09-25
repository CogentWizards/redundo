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
    assert summary.spans_with_conversation_id == 1
    assert summary.spans_fallback_to_trace_id == 0


def test_falls_back_to_trace_id_when_conversation_id_absent():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, summary = convert(traces_document(spans))
    assert records[0]["task_id"] == "trace-1"
    assert summary.spans_fallback_to_trace_id == 1
    assert any("cross-trace rework not detected" in n for n in summary.notes())


def test_different_conversation_ids_on_unrelated_spans_each_get_their_own_task_id():
    # Two root-level spans in one trace, no ancestor relationship between
    # them, each with its own real, distinct conversation id. This used
    # to force both back to the shared trace ID ("ambiguous, don't
    # guess") -- but per-span resolution means there is nothing to guess:
    # each span's own stated identity is a real answer, not a competing
    # claim about the other's. Two different real ids on two unrelated
    # spans is just two different real tasks, confirmed against a real
    # capture (a Hermes subagent's spans carry their own session id
    # directly, with no ancestor link to the parent conversation's own
    # spans at all -- see docs/openinference.md).
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "gen_ai.conversation.id": "conv-a"}),
        span("s2", trace_id="trace-1", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "bye",
                         "gen_ai.conversation.id": "conv-b"}),
    ]
    records, summary = convert(traces_document(spans))
    assert {r["task_id"] for r in records} == {"conv-a", "conv-b"}
    assert summary.spans_with_conversation_id == 2
    assert summary.spans_fallback_to_trace_id == 0


def test_never_synthesizes_a_task_id():
    # No conversation.id anywhere; task_id must be exactly the trace ID,
    # never anything derived/hashed/invented.
    spans = [
        span("s1", trace_id="exact-trace-id-value", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["task_id"] == "exact-trace-id-value"


def test_session_id_used_as_a_conversation_id_fallback():
    # Real for Google ADK: openinference-instrumentation-google-adk maps
    # ADK's own session id onto session.id, never gen_ai.conversation.id.
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "session.id": "adk-session-1"}),
    ]
    records, summary = convert(traces_document(spans))
    assert records[0]["task_id"] == "adk-session-1"
    assert summary.spans_with_conversation_id == 1


def test_conversation_id_preferred_over_session_id_when_both_present():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "gen_ai.conversation.id": "conv-1", "session.id": "sess-1"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["task_id"] == "conv-1"


def test_different_session_and_conversation_ids_on_unrelated_spans_each_get_their_own_task_id():
    spans = [
        span("s1", trace_id="trace-1", start=0,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi",
                         "gen_ai.conversation.id": "conv-1"}),
        span("s2", trace_id="trace-1", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "bye",
                         "session.id": "sess-2"}),
    ]
    records, summary = convert(traces_document(spans))
    assert {r["task_id"] for r in records} == {"conv-1", "sess-2"}
    assert summary.spans_with_conversation_id == 2


# --- parent_task_id (real cross-task delegation link) ---------------------

def test_parent_task_id_read_from_hermes_subagent_attribute():
    spans = [
        span("s1", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
            "hermes.subagent.parent_session_id": "parent-session-1",
        }),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["metadata"]["parent_task_id"] == "parent-session-1"


def test_parent_task_id_absent_when_source_never_reports_one():
    spans = [
        span("s1", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
        }),
    ]
    records, _ = convert(traces_document(spans))
    assert "parent_task_id" not in records[0]["metadata"]


def test_parent_task_id_also_carried_on_tool_call_and_result():
    spans = [
        span("s1", start=0, attributes={
            "openinference.span.kind": "TOOL", "tool.name": "search",
            "input.value": "{}", "output.value": "{}",
            "hermes.subagent.parent_session_id": "parent-session-1",
        }),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["metadata"]["parent_task_id"] == "parent-session-1"
    assert records[1]["metadata"]["parent_task_id"] == "parent-session-1"


def test_subagent_delegation_gets_its_own_task_id_and_inherited_parent_task_id():
    # The real shape confirmed against a live Hermes capture (delegate_task
    # spawning two subagents): a parent conversation and two subagent
    # branches, all inside ONE trace. Each subagent's own AGENT-kind
    # wrapper span carries its own session id AND
    # hermes.subagent.parent_session_id pointing at the parent's session
    # id; the LLM/TOOL spans actually nested inside it carry their own
    # session id too (matching the parent, i.e. their own subagent's), but
    # NOT their own parent_session_id -- that has to be inherited from the
    # AGENT wrapper via an ancestor walk. The AGENT wrapper's own real
    # parent span is never captured in the real data either (confirmed:
    # its parentSpanId pointed at a span id that appeared nowhere in the
    # export), so this fixture deliberately leaves it unset (a root span)
    # rather than inventing an uncaptured ancestor.
    spans = [
        span("parent-llm", trace_id="t1", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "delegate to two subagents",
            "gen_ai.conversation.id": "parent-session",
        }),
        span("agent-a", trace_id="t1", start=10, attributes={
            "openinference.span.kind": "AGENT",
            "gen_ai.conversation.id": "subagent-a",
            "hermes.subagent.parent_session_id": "parent-session",
        }),
        span("a-llm1", trace_id="t1", parent_span_id="agent-a", start=20, attributes={
            "openinference.span.kind": "LLM", "input.value": "research battery manufacturing",
            "gen_ai.conversation.id": "subagent-a",
        }),
        span("agent-b", trace_id="t1", start=10, attributes={
            "openinference.span.kind": "AGENT",
            "gen_ai.conversation.id": "subagent-b",
            "hermes.subagent.parent_session_id": "parent-session",
        }),
        span("b-llm1", trace_id="t1", parent_span_id="agent-b", start=20, attributes={
            "openinference.span.kind": "LLM", "input.value": "research battery environmental impact",
            "gen_ai.conversation.id": "subagent-b",
        }),
    ]
    records, _ = convert(traces_document(spans))
    assert len(records) == 3  # the two AGENT wrappers are skipped, never converted

    parent_record = next(r for r in records if r["task_id"] == "parent-session")
    a_record = next(r for r in records if r["task_id"] == "subagent-a")
    b_record = next(r for r in records if r["task_id"] == "subagent-b")

    # (a) each subagent's events get their own distinct task_id, not the
    # parent's and not each other's.
    assert len({parent_record["task_id"], a_record["task_id"], b_record["task_id"]}) == 3

    # (b) parent_task_id correctly resolves for LLM events nested under a
    # subagent's AGENT wrapper, inherited from that wrapper, even though
    # the LLM/TOOL span itself carries no hermes.subagent.parent_session_id.
    assert "parent_task_id" not in parent_record["metadata"]
    assert a_record["metadata"]["parent_task_id"] == "parent-session"
    assert b_record["metadata"]["parent_task_id"] == "parent-session"


# --- step_index does not collide across a task's own multiple traces -----

def test_step_index_stays_unique_across_two_traces_sharing_one_task_id():
    # A task_id can legitimately span more than one physical trace (e.g. a
    # source that gives one CLI invocation its own trace per turn but a
    # stable session id across turns, confirmed for Hermes). Before this
    # fix, step_index was assigned per trace_id, so two traces sharing one
    # task_id would each start their own step_index at 0 -- colliding
    # within the task and silently corrupting lineage.TaskLineage's
    # step-indexed internals (see redundo.analyzer.lineage.TaskLineage.build,
    # which assumes step_index is unique per task_id).
    spans = [
        span("s1", trace_id="trace-a", start=0, attributes={
            "openinference.span.kind": "TOOL", "tool.name": "calculate",
            "input.value": '{"expr": "47*89"}',
            "gen_ai.conversation.id": "task-x",
        }),
        span("s2", trace_id="trace-b", start=1000, attributes={
            "openinference.span.kind": "TOOL", "tool.name": "calculate",
            "input.value": '{"expr": "47*89"}',  # verbatim repeat, different trace
            "gen_ai.conversation.id": "task-x",
        }),
    ]
    records, _ = convert(traces_document(spans))
    tool_calls = [r for r in records if r["event_type"] == "tool_call"]
    assert len(tool_calls) == 2
    assert all(r["task_id"] == "task-x" for r in tool_calls)

    step_indices = [r["step_index"] for r in records]
    assert len(step_indices) == len(set(step_indices)), (
        f"step_index collided across traces sharing one task_id: {step_indices}"
    )

    # And the actual point of fixing this: a genuine repeat spanning the
    # two traces is now findable as a real candidate pair, not silently
    # lost to a corrupted lineage index.
    from redundo.analyzer.cycles import find_candidate_pairs
    from redundo.analyzer.schema import Event

    events = [Event.from_dict(r) for r in records]
    pairs = find_candidate_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.task_id == "task-x"
    assert pairs[0].repeat.task_id == "task-x"


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


def test_workflow_defaults_to_main_with_no_agent_or_chain_ancestor():
    spans = [span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"})]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "main"
    assert records[0]["metadata"]["workflow_basis"] == "no_workflow_ancestor"


def test_workflow_prefers_agent_name_attribute_over_decorated_span_name():
    # Google ADK's own AGENT-kind span is named "agent_run [<name>]"
    # (decorated) but carries the bare name in agent.name -- confirmed
    # against openinference-instrumentation-google-adk's real source
    # (_wrappers.py). The attribute must win over the span's own name.
    spans = [
        span("agent", start=0, name="agent_run [search_agent]",
             attributes={"openinference.span.kind": "AGENT", "agent.name": "search_agent"}),
        span("llm1", parent_span_id="agent", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "search_agent"
    assert records[0]["metadata"]["workflow_basis"] == "agent_name_attribute"


def test_workflow_falls_back_to_span_name_without_agent_name_attribute():
    spans = [
        span("agent", start=0, name="research_agent", attributes={"openinference.span.kind": "AGENT"}),
        span("llm1", parent_span_id="agent", start=1,
             attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ]
    records, _ = convert(traces_document(spans))
    assert records[0]["workflow"] == "research_agent"
    assert records[0]["metadata"]["workflow_basis"] == "span_name"


# --- model derivation for TOOL spans ----------------------------------

def test_tool_call_gets_model_from_preceding_llm_span_in_same_task():
    spans = [
        span("llm1", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi", "llm.model_name": "gpt-5",
        }),
        span("tool1", start=1, attributes={
            "openinference.span.kind": "TOOL", "input.value": "args", "output.value": "result",
        }),
    ]
    records, _ = convert(traces_document(spans))
    call = next(r for r in records if r["event_type"] == "tool_call")
    result = next(r for r in records if r["event_type"] == "tool_result")
    assert call["model"] == "gpt-5"
    assert call["metadata"]["model_basis"] == "preceding_llm_call"
    assert result["model"] == "gpt-5"
    assert result["metadata"]["model_basis"] == "preceding_llm_call"


def test_tool_call_before_any_llm_span_falls_back_to_the_following_one():
    # Confirmed real shape (Claude Code): a task's step 0 can genuinely be
    # a tool call, its first llm_call only appearing later. The nearest
    # LLM span is still a real fact about this task's own execution, just
    # found by looking forward instead of back.
    spans = [
        span("tool1", start=0, attributes={
            "openinference.span.kind": "TOOL", "input.value": "args", "output.value": "result",
        }),
        span("llm1", start=1, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi", "llm.model_name": "gpt-5",
        }),
    ]
    records, _ = convert(traces_document(spans))
    call = next(r for r in records if r["event_type"] == "tool_call")
    assert call["model"] == "gpt-5"
    assert call["metadata"]["model_basis"] == "following_llm_call"


def test_tool_call_with_no_llm_span_anywhere_in_its_workflow_has_no_model():
    spans = [
        span("tool1", start=0, attributes={
            "openinference.span.kind": "TOOL", "input.value": "args", "output.value": "result",
        }),
    ]
    records, _ = convert(traces_document(spans))
    call = next(r for r in records if r["event_type"] == "tool_call")
    assert call["model"] is None
    assert "model_basis" not in call["metadata"]


def test_subagent_model_does_not_leak_across_workflows():
    # Two AGENT-wrapped branches in the same task (a real, confirmed shape
    # -- see the "cross-task/branch" tests above): branch A's model must
    # never leak onto branch B's tool call just because branch A's LLM
    # span happens to come first chronologically.
    spans = [
        span("agent-a", start=0, name="agent_a", attributes={"openinference.span.kind": "AGENT"}),
        span("llm-a", parent_span_id="agent-a", start=1, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi", "llm.model_name": "gpt-5-mini",
        }),
        span("agent-b", start=2, name="agent_b", attributes={"openinference.span.kind": "AGENT"}),
        span("tool-b", parent_span_id="agent-b", start=3, attributes={
            "openinference.span.kind": "TOOL", "input.value": "args", "output.value": "result",
        }),
    ]
    records, _ = convert(traces_document(spans))
    tool_b = next(r for r in records if r["event_type"] == "tool_call")
    assert tool_b["workflow"] == "agent_b"
    assert tool_b["model"] is None


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
    # All 5 spans in the fixture (including the 2 skipped-by-kind ones --
    # resolution runs over every span, not just kept ones) share one real
    # conversation id.
    assert summary.spans_with_conversation_id == 5
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
