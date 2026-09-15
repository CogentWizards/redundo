"""api_request log records with real cost_usd but no matching
claude_code.llm_request span at all. Confirmed against a real capture
(see CogentWizards/redundo#24): Claude Code's own session-title
generation call, and a session's very first call under Agent SDK/
streaming transport, dispatched before span instrumentation starts.
_synthesize_orphaned_cost_records turns these into their own degraded
llm_call records instead of letting that spend silently vanish.
"""

from redundo.adapter.sources.claude_code import convert_claude_code as convert
from helpers import log_record, logs_document, span, traces_document

SESSION = "sess-1"


def _interaction(span_id="interaction", prompt="do the thing", start=0, end=1000):
    return span(
        span_id, name="claude_code.interaction", start=start, end=end,
        attributes={"session.id": SESSION, "user_prompt": prompt},
    )


def _llm_request(span_id, parent, start, end, **extra_attrs):
    attrs = {
        "session.id": SESSION, "model": "claude-sonnet-5", "input_tokens": 100,
        "output_tokens": 50, "success": True, "request_id": f"req_{span_id}",
        **extra_attrs,
    }
    return span(span_id, parent_span_id=parent, name="claude_code.llm_request",
                start=start, end=end, attributes=attrs)


def _api_request_log(request_id, cost_usd, session=SESSION, **extra_attrs):
    return log_record(attributes={
        "event.name": "api_request", "session.id": session,
        "request_id": request_id, "cost_usd": cost_usd,
        **extra_attrs,
    })


def test_orphaned_api_request_becomes_its_own_llm_call():
    spans = [_interaction(), _llm_request("llm1", "interaction", 0, 100)]
    logs = [
        _api_request_log("req_llm1", 0.0123),
        _api_request_log("req_orphan", 0.000959, model="claude-haiku-4-5",
                          query_source="generate_session_title"),
    ]
    records, summary = convert([traces_document(spans), logs_document(logs)])
    llm_calls = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_calls) == 2

    orphan = [r for r in llm_calls if r["metadata"]["request_id"] == "req_orphan"][0]
    assert orphan["cost_usd"] == 0.000959
    assert orphan["model"] == "claude-haiku-4-5"
    assert orphan["metadata"]["content_basis"] == "log_only_no_span"
    assert orphan["metadata"]["synthesized_cost_only"] is True
    assert orphan["metadata"]["query_source"] == "generate_session_title"
    assert orphan["parent_id"] is None

    assert summary.records_synthesized_from_log_only == 1
    assert summary.cost_usd_synthesized_from_log_only == 0.000959
    assert any("synthesized from an api_request log" in n for n in summary.notes())


def test_orphan_gets_a_real_task_id_matching_its_session():
    spans = [_interaction(), _llm_request("llm1", "interaction", 0, 100)]
    logs = [
        _api_request_log("req_llm1", 0.0123),
        _api_request_log("req_orphan", 0.001),
    ]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    orphan = [r for r in records if r["metadata"].get("request_id") == "req_orphan"][0]
    assert orphan["task_id"] == SESSION


def test_orphan_step_index_continues_after_every_real_record_in_its_task():
    spans = [_interaction(), _llm_request("llm1", "interaction", 0, 100)]
    logs = [
        _api_request_log("req_llm1", 0.0123),
        _api_request_log("req_orphan", 0.001),
    ]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    real_record = [r for r in records if r["metadata"].get("request_id") == "req_llm1"][0]
    orphan = [r for r in records if r["metadata"].get("request_id") == "req_orphan"][0]
    assert orphan["step_index"] > real_record["step_index"]


def test_two_orphans_get_distinct_hashes_and_step_indices():
    logs = [
        _api_request_log("req_a", 0.001, model="claude-haiku-4-5"),
        _api_request_log("req_b", 0.002, model="claude-opus-5"),
    ]
    records, summary = convert([logs_document(logs)])
    assert len(records) == 2
    assert records[0]["content_hash"] != records[1]["content_hash"]
    assert {r["step_index"] for r in records} == {0, 1}
    assert summary.records_synthesized_from_log_only == 2
    assert summary.cost_usd_synthesized_from_log_only == 0.003


def test_matched_api_request_is_never_synthesized_twice():
    # The real, already-covered case: an api_request that DID match a
    # span must not also produce a duplicate synthesized record.
    spans = [_interaction(), _llm_request("llm1", "interaction", 0, 100)]
    logs = [_api_request_log("req_llm1", 0.0123)]
    records, summary = convert([traces_document(spans), logs_document(logs)])
    assert len(records) == 1
    assert summary.records_synthesized_from_log_only == 0


def test_no_session_id_falls_back_to_request_id_as_its_own_task():
    logs = [log_record(attributes={
        "event.name": "api_request", "request_id": "req_orphan", "cost_usd": 0.001,
    })]
    records, _ = convert([logs_document(logs)])
    assert len(records) == 1
    assert records[0]["task_id"] == "req_orphan"
