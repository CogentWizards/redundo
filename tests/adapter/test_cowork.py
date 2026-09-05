from redundo.adapter.sources.cowork import convert_cowork
from helpers import log_record, logs_document

SESSION = "sess-1"


def rec(event_name, seq, prompt_id=None, **extra):
    attrs = {
        "event.name": event_name, "session.id": SESSION, "event.sequence": seq,
        "event.timestamp": f"t{seq}",
    }
    if prompt_id:
        attrs["prompt.id"] = prompt_id
    attrs.update(extra)
    return log_record(attributes=attrs)


# --- llm_call from api_request -------------------------------------------

def test_api_request_becomes_llm_call_with_direct_cost():
    records = [
        rec("user_prompt", 1, prompt_id="p1", prompt="do the thing"),
        rec("api_request", 2, prompt_id="p1", model="claude-sonnet-5", cost_usd=0.05,
            input_tokens=100, output_tokens=50, request_id="req1"),
    ]
    out, summary = convert_cowork([logs_document(records)])
    assert len(out) == 1
    r = out[0]
    assert r["event_type"] == "llm_call"
    assert r["task_id"] == SESSION
    assert r["cost_usd"] == 0.05
    assert r["tokens_in"] == 100
    assert r["tokens_out"] == 50
    assert r["outcome"] == "ok"
    assert r["metadata"]["content_basis"] == "prompt"


def test_api_error_becomes_llm_call_with_error_outcome():
    records = [rec("api_error", 1, prompt_id="p1", model="claude-sonnet-5", error="boom")]
    out, _ = convert_cowork([logs_document(records)])
    assert len(out) == 1
    assert out[0]["outcome"] == "error"
    assert out[0]["cost_usd"] is None


def test_only_first_api_request_per_prompt_gets_prompt_content():
    records = [
        rec("user_prompt", 1, prompt_id="p1", prompt="do the thing"),
        rec("api_request", 2, prompt_id="p1", request_id="req1"),
        rec("tool_result", 3, prompt_id="p1", tool_name="search", tool_input='{"q":"x"}'),
        rec("api_request", 4, prompt_id="p1", request_id="req2"),
    ]
    out, _ = convert_cowork([logs_document(records)])
    llm_calls = [r for r in out if r["event_type"] == "llm_call"]
    assert len(llm_calls) == 2
    assert llm_calls[0]["metadata"]["content_basis"] == "prompt"
    assert llm_calls[1]["metadata"]["content_basis"] == "opaque"
    assert llm_calls[0]["content_hash"] != llm_calls[1]["content_hash"]


def test_redacted_prompt_not_used_as_content():
    records = [
        rec("user_prompt", 1, prompt_id="p1", prompt="<REDACTED>"),
        rec("api_request", 2, prompt_id="p1", request_id="req1"),
    ]
    out, _ = convert_cowork([logs_document(records)])
    assert out[0]["metadata"]["content_basis"] == "opaque"


def test_response_hash_joined_from_assistant_response():
    records = [
        rec("api_request", 1, prompt_id="p1", request_id="req1"),
        rec("assistant_response", 2, prompt_id="p1", request_id="req1", response="the answer"),
    ]
    out, summary = convert_cowork([logs_document(records)])
    assert "response_hash" in out[0]["metadata"]
    assert summary.records_with_response_hash == 1


def test_redacted_response_not_used_for_response_hash():
    records = [
        rec("api_request", 1, prompt_id="p1", request_id="req1"),
        rec("assistant_response", 2, prompt_id="p1", request_id="req1", response="<REDACTED>"),
    ]
    out, _ = convert_cowork([logs_document(records)])
    assert "response_hash" not in out[0]["metadata"]


# --- tool_call from tool_result -------------------------------------------

def test_tool_result_becomes_tool_call_only_no_result_record():
    records = [rec("tool_result", 1, prompt_id="p1", tool_name="Read",
                    tool_input='{"file_path":"/a.txt"}', success=True)]
    out, summary = convert_cowork([logs_document(records)])
    assert len(out) == 1
    assert out[0]["event_type"] == "tool_call"
    assert out[0]["outcome"] == "ok"
    assert out[0]["metadata"]["content_basis"] == "tool_input"
    assert "tool_result content is never available" in " ".join(summary.notes())


def test_two_identical_tool_calls_get_identical_hash():
    records = [
        rec("tool_result", 1, prompt_id="p1", tool_name="Read",
            tool_input='{"file_path":"/a.txt"}', success=True),
        rec("tool_result", 2, prompt_id="p1", tool_name="Read",
            tool_input='{"file_path":"/a.txt"}', success=True),
    ]
    out, _ = convert_cowork([logs_document(records)])
    assert out[0]["content_hash"] == out[1]["content_hash"]


def test_mcp_tool_qualified_from_tool_parameters():
    records = [rec(
        "tool_result", 1, prompt_id="p1", tool_name="mcp_tool",
        tool_input='{"pair":"USD/EUR"}',
        tool_parameters='{"mcp_server_name":"fx","mcp_tool_name":"get_rate"}',
        success=True,
    )]
    out, _ = convert_cowork([logs_document(records)])
    assert out[0]["name"] == "mcp__fx__get_rate"
    assert out[0]["metadata"]["mcp"] is True


def test_tool_call_without_tool_input_is_opaque():
    records = [rec("tool_result", 1, prompt_id="p1", tool_name="Read", success=True)]
    out, _ = convert_cowork([logs_document(records)])
    assert out[0]["metadata"]["content_basis"] == "opaque"


# --- rejected tool_decision --------------------------------------------

def test_rejected_tool_decision_becomes_tool_call():
    records = [rec("tool_decision", 1, prompt_id="p1", tool_name="Bash", decision="reject",
                    source="user_reject")]
    out, _ = convert_cowork([logs_document(records)])
    assert len(out) == 1
    assert out[0]["event_type"] == "tool_call"
    assert out[0]["outcome"] == "error"
    assert out[0]["metadata"]["rejected_before_execution"] is True


def test_accepted_tool_decision_produces_no_record_by_itself():
    records = [
        rec("tool_decision", 1, prompt_id="p1", tool_name="Bash", decision="accept",
            source="user_temporary"),
        rec("tool_result", 2, prompt_id="p1", tool_name="Bash", tool_input='{"cmd":"ls"}',
            success=True),
    ]
    out, _ = convert_cowork([logs_document(records)])
    # Only the tool_result becomes a record -- the accept decision must not
    # also produce one, or every successful call would be double-counted.
    assert len(out) == 1
    assert out[0]["event_type"] == "tool_call"


# --- ordering / parent_id / task grouping ---------------------------------

def test_events_ordered_by_sequence_parent_always_none():
    records = [
        rec("api_request", 2, prompt_id="p1", request_id="req1"),
        rec("tool_result", 1, prompt_id="p1", tool_name="Read", tool_input='{"a":1}'),
    ]
    out, _ = convert_cowork([logs_document(records)])
    assert [r["event_type"] for r in out] == ["tool_call", "llm_call"]
    assert [r["step_index"] for r in out] == [0, 1]
    assert all(r["parent_id"] is None for r in out)


def test_missing_session_id_is_dropped_not_guessed():
    records = [log_record(attributes={
        "event.name": "api_request", "event.sequence": 1, "request_id": "r1",
    })]
    out, summary = convert_cowork([logs_document(records)])
    assert out == []
    assert summary.sessions_total == 0


def test_unrecognized_event_kind_is_counted_not_dropped_silently():
    records = [rec("mcp_server_connection", 1, prompt_id="p1")]
    out, summary = convert_cowork([logs_document(records)])
    assert out == []
    assert summary.skipped_by_kind == {"mcp_server_connection": 1}
