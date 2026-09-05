from datetime import datetime, timezone

from redundo.adapter.sources.claude_code import convert_claude_code as convert
from helpers import log_record, logs_document, span, span_event, traces_document

SESSION = "sess-1"


def _iso(nanos):
    """Inverse of convert.py's own _iso_timestamp -- keeps a test's span
    start_time_unix_nano and a log record's event.timestamp mutually
    consistent, the way real captured data is.
    """
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).isoformat()


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


def _tool(span_id, parent, tool_name, start, end, events=None, **extra_attrs):
    attrs = {"session.id": SESSION, "tool_name": tool_name, **extra_attrs}
    return span(span_id, parent_span_id=parent, name="claude_code.tool",
                start=start, end=end, attributes=attrs, events=events)


def _tool_execution(span_id, parent, success=True, start=0, end=10):
    return span(span_id, parent_span_id=parent, name="claude_code.tool.execution",
                start=start, end=end, attributes={"session.id": SESSION, "success": success})


# --- basic conversion ---------------------------------------------------

def test_llm_call_with_prompt_hashes_interaction_user_prompt():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 10, 500),
    ]
    records, summary = convert([traces_document(spans)])
    assert len(records) == 1
    r = records[0]
    assert r["event_type"] == "llm_call"
    assert r["task_id"] == SESSION
    assert r["metadata"]["content_basis"] == "prompt"
    assert r["metadata"]["task_id_source"] == "session_id"
    assert r["model"] == "claude-sonnet-5"
    assert r["tokens_in"] == 100
    assert r["tokens_out"] == 50
    assert r["outcome"] == "ok"


def test_redacted_user_prompt_is_not_used_as_content():
    spans = [
        _interaction(prompt="<REDACTED>"),
        _llm_request("llm1", "interaction", 10, 500),
    ]
    records, _ = convert([traces_document(spans)])
    assert records[0]["metadata"]["content_basis"] == "opaque"


def test_built_in_tool_call_and_result_pair():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 0, 100),
        _tool(
            "t1", "interaction", "Read", 110, 200,
            file_path="/a.txt",
            events=[span_event("tool.output", 190, {"content": "file contents"})],
        ),
        _tool_execution("t1exec", "t1", success=True, start=110, end=190),
    ]
    records, _ = convert([traces_document(spans)])
    by_type = {r["event_type"]: r for r in records}
    assert by_type["tool_call"]["name"] == "Read"
    assert by_type["tool_call"]["metadata"]["content_basis"] == "tool_input"
    assert by_type["tool_result"]["outcome"] == "ok"
    assert by_type["tool_result"]["parent_id"] == by_type["tool_call"]["step_index"]


def test_bash_tool_output_uses_output_key_not_content():
    # Confirmed empirically against a real captured session: Bash's
    # tool.output event carries its result under "output", not "content"
    # like Read's does -- the same span event name, a different attribute
    # key per tool.
    spans = [
        _interaction(),
        _tool(
            "t1", "interaction", "Bash", 10, 60,
            full_command="cat notes.txt",
            events=[span_event("tool.output", 55, {"output": "file contents"})],
        ),
        _tool_execution("t1exec", "t1", success=True, start=10, end=55),
    ]
    records, _ = convert([traces_document(spans)])
    result = [r for r in records if r["event_type"] == "tool_result"][0]
    assert result["metadata"]["content_basis"] == "prompt"
    assert result["content_hash"] is not None


def test_tool_call_with_no_output_event_gets_no_result_record():
    # No tool.output event and no logs join -- genuinely nothing to
    # correlate. Must not fabricate a result record (see convert.py's
    # extensive comment on why an opaque result hash is unsafe).
    spans = [
        _interaction(),
        _tool("t1", "interaction", "mcp__server__tool", 10, 50),
        _tool_execution("t1exec", "t1", success=True, start=10, end=45),
    ]
    records, _ = convert([traces_document(spans)])
    types = [r["event_type"] for r in records]
    assert types == ["tool_call"]


# --- MCP argument join via logs ------------------------------------------

def _mcp_tool_result_log(seq, tool_name="mcp_tool", tool_input='{"pair":"USD/EUR"}',
                          mcp_server="fx", mcp_tool="get_rate"):
    return log_record(attributes={
        "event.name": "tool_result", "session.id": SESSION, "event.sequence": seq,
        "tool_name": tool_name, "tool_input": tool_input,
        "tool_parameters": f'{{"mcp_server_name":"{mcp_server}","mcp_tool_name":"{mcp_tool}"}}',
    })


def test_mcp_tool_call_gets_arguments_from_logs_join():
    spans = [
        _interaction(),
        _tool("t1", "interaction", "mcp__fx__get_rate", 10, 50),
        _tool_execution("t1exec", "t1", success=True, start=10, end=45),
    ]
    logs = [_mcp_tool_result_log(1)]
    records, summary = convert([traces_document(spans), logs_document(logs)])
    call = records[0]
    assert call["metadata"]["content_basis"] == "tool_input"
    assert call["metadata"]["mcp"] is True
    assert summary.mcp_tool_calls_with_arguments == 1


def test_mcp_tool_call_without_logs_falls_back_to_opaque():
    spans = [
        _interaction(),
        _tool("t1", "interaction", "mcp__fx__get_rate", 10, 50),
        _tool_execution("t1exec", "t1", success=True, start=10, end=45),
    ]
    records, summary = convert([traces_document(spans)])
    assert records[0]["metadata"]["content_basis"] == "opaque"
    assert summary.mcp_tool_calls_with_arguments == 0


def test_two_identical_mcp_calls_get_identical_call_hash():
    spans = [
        _interaction(),
        _tool("t1", "interaction", "mcp__fx__get_rate", 10, 50),
        _tool_execution("t1exec", "t1", success=True, start=10, end=45),
        _tool("t2", "interaction", "mcp__fx__get_rate", 60, 100),
        _tool_execution("t2exec", "t2", success=True, start=60, end=95),
    ]
    logs = [_mcp_tool_result_log(1), _mcp_tool_result_log(2)]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    calls = [r for r in records if r["event_type"] == "tool_call"]
    assert len(calls) == 2
    assert calls[0]["content_hash"] == calls[1]["content_hash"]


def test_positional_join_skipped_on_tool_name_mismatch():
    spans = [
        _interaction(),
        _tool("t1", "interaction", "mcp__fx__get_rate", 10, 50),
        _tool_execution("t1exec", "t1", success=True, start=10, end=45),
    ]
    # Logs signal has a tool_result for a *different* tool at this
    # position -- positional join must refuse rather than mis-attribute.
    logs = [_mcp_tool_result_log(1, tool_name="Read")]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    assert records[0]["metadata"]["content_basis"] == "opaque"


# --- first-llm-request-per-interaction -----------------------------------

def test_only_first_llm_request_in_interaction_gets_prompt_content():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 0, 100),
        _tool("t1", "interaction", "Read", 110, 200, file_path="/a.txt"),
        _tool_execution("t1exec", "t1", start=110, end=190),
        _llm_request("llm2", "interaction", 210, 300),
    ]
    records, _ = convert([traces_document(spans)])
    llm_calls = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_calls) == 2
    assert llm_calls[0]["metadata"]["content_basis"] == "prompt"
    assert llm_calls[1]["metadata"]["content_basis"] == "opaque"
    # Different opaque hashes -- must not coincidentally collide.
    assert llm_calls[0]["content_hash"] != llm_calls[1]["content_hash"]


# --- cost via request_id join --------------------------------------------

def test_llm_call_cost_joined_from_api_request_log():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 0, 100),
    ]
    logs = [log_record(attributes={
        "event.name": "api_request", "session.id": SESSION,
        "request_id": "req_llm1", "cost_usd": 0.0123,
    })]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    assert records[0]["cost_usd"] == 0.0123


def test_llm_call_cost_none_without_logs():
    spans = [_interaction(), _llm_request("llm1", "interaction", 0, 100)]
    records, _ = convert([traces_document(spans)])
    assert records[0]["cost_usd"] is None


# --- parent chaining ------------------------------------------------------

def test_siblings_under_interaction_chain_sequentially():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 0, 100),
        _tool("t1", "interaction", "Read", 50, 150, file_path="/a.txt"),
        _tool_execution("t1exec", "t1", start=50, end=140),
        _llm_request("llm2", "interaction", 160, 260),
    ]
    records, _ = convert([traces_document(spans)])
    by_step = {r["step_index"]: r for r in records}
    # step0: llm1 (root); step1: tool_call Read (chains to llm1); step2:
    # llm2 (chains to Read, the last sibling processed).
    assert by_step[0]["event_type"] == "llm_call" and by_step[0]["parent_id"] is None
    assert by_step[1]["event_type"] == "tool_call" and by_step[1]["parent_id"] == 0
    last_llm = [r for r in records if r["event_type"] == "llm_call"][-1]
    tool_call = [r for r in records if r["event_type"] == "tool_call"][0]
    assert last_llm["parent_id"] == tool_call["step_index"]


def test_cross_interaction_events_chain_within_session():
    # Two separate interactions (separate traces), same session -- the
    # second interaction's root event has no real ancestor, so it must
    # fall through to waste_analyzer's own linear fallback (parent_id
    # left as None; step_index keeps counting from the first
    # interaction, not reset).
    interaction2 = _interaction(span_id="interaction2", start=1000, end=2000)
    spans = [
        _interaction(start=0, end=500),
        _llm_request("llm1", "interaction", 0, 100),
        interaction2,
        _llm_request("llm2", "interaction2", 1000, 1100),
    ]
    records, _ = convert([traces_document(spans)])
    assert [r["step_index"] for r in records] == [0, 1]
    assert records[1]["parent_id"] is None  # bridged by the analyzer's own fallback
    assert records[0]["task_id"] == records[1]["task_id"] == SESSION


# --- session / task_id -----------------------------------------------------

def test_session_id_missing_falls_back_to_trace_id():
    s = span("llm1", trace_id="trace-xyz", name="claude_code.llm_request",
             start=0, end=10, attributes={"model": "m", "success": True})
    records, summary = convert([traces_document([s])])
    assert records[0]["task_id"] == "trace-xyz"
    assert records[0]["metadata"]["task_id_source"] == "trace_id_fallback"
    assert summary.sessions_with_session_id == 0


# --- unsupported span kinds -------------------------------------------------

def test_hook_span_is_skipped_and_counted():
    spans = [
        _interaction(),
        _llm_request("llm1", "interaction", 0, 100),
        span("h1", parent_span_id="interaction", name="claude_code.hook",
             start=10, end=20, attributes={"session.id": SESSION}),
    ]
    records, summary = convert([traces_document(spans)])
    assert len(records) == 1
    assert summary.skipped_by_kind == {"claude_code.hook": 1}


# --- windowed prompt fallback (Agent SDK / streaming: no interaction span) -

def _user_prompt_log(prompt, at_nanos, seq):
    return log_record(attributes={
        "event.name": "user_prompt", "session.id": SESSION,
        "event.sequence": seq, "event.timestamp": _iso(at_nanos), "prompt": prompt,
    })


def test_windowed_prompt_recovers_content_with_no_interaction_span():
    # No claude_code.interaction span anywhere -- the Agent SDK/streaming
    # signature. The llm_request span has no parent_span_id at all.
    spans = [span("llm1", name="claude_code.llm_request", start=1_000_000_000, end=1_100_000_000,
                   attributes={"session.id": SESSION, "model": "m", "success": True})]
    logs = [_user_prompt_log("do the thing", at_nanos=500_000_000, seq=1)]
    records, summary = convert([traces_document(spans), logs_document(logs)])
    assert len(records) == 1
    assert records[0]["metadata"]["content_basis"] == "prompt_windowed"
    assert summary.sessions_without_interaction_span == 1
    assert summary.records_with_windowed_prompt_content == 1


def test_windowed_prompt_only_claims_first_llm_request_per_window():
    spans = [
        span("llm1", name="claude_code.llm_request", start=1_000_000_000, end=1_100_000_000,
             attributes={"session.id": SESSION, "model": "m", "success": True}),
        span("llm2", name="claude_code.llm_request", start=1_200_000_000, end=1_300_000_000,
             attributes={"session.id": SESSION, "model": "m", "success": True}),
    ]
    logs = [_user_prompt_log("do the thing", at_nanos=500_000_000, seq=1)]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    llm_calls = sorted(records, key=lambda r: r["step_index"])
    assert llm_calls[0]["metadata"]["content_basis"] == "prompt_windowed"
    assert llm_calls[1]["metadata"]["content_basis"] == "opaque"


def test_windowed_prompt_second_turn_gets_second_window():
    spans = [
        span("llm1", name="claude_code.llm_request", start=1_000_000_000, end=1_100_000_000,
             attributes={"session.id": SESSION, "model": "m", "success": True}),
        span("llm2", name="claude_code.llm_request", start=3_000_000_000, end=3_100_000_000,
             attributes={"session.id": SESSION, "model": "m", "success": True}),
    ]
    logs = [
        _user_prompt_log("first turn", at_nanos=500_000_000, seq=1),
        _user_prompt_log("second turn", at_nanos=2_000_000_000, seq=2),
    ]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    llm_calls = sorted(records, key=lambda r: r["step_index"])
    assert llm_calls[0]["metadata"]["content_basis"] == "prompt_windowed"
    assert llm_calls[1]["metadata"]["content_basis"] == "prompt_windowed"
    assert llm_calls[0]["content_hash"] != llm_calls[1]["content_hash"]


def test_windowed_fallback_never_fires_when_interaction_span_present():
    # Even an orphan llm_request (parent_span_id=None, e.g. a real
    # session-title-generation call) inside an otherwise-normal direct-CLI
    # session must not pick up an unrelated window's prompt text -- the
    # fallback is gated on the whole session having zero interaction
    # spans, not on any individual span lacking a parent.
    spans = [
        _interaction(prompt="the real turn"),
        _llm_request("llm1", "interaction", 10, 100),
        span("orphan", name="claude_code.llm_request", start=5, end=6,
             attributes={"session.id": SESSION, "model": "m", "success": True}),
    ]
    logs = [_user_prompt_log("decoy prompt", at_nanos=0, seq=1)]
    records, summary = convert([traces_document(spans), logs_document(logs)])
    assert summary.sessions_without_interaction_span == 0
    # Neither llm_call may have used the windowed path.
    assert all(r["metadata"]["content_basis"] != "prompt_windowed" for r in records
               if r["event_type"] == "llm_call")


def test_windowed_prompt_redacted_not_used():
    spans = [span("llm1", name="claude_code.llm_request", start=1_000_000_000, end=1_100_000_000,
                   attributes={"session.id": SESSION, "model": "m", "success": True})]
    logs = [_user_prompt_log("<REDACTED>", at_nanos=500_000_000, seq=1)]
    records, _ = convert([traces_document(spans), logs_document(logs)])
    assert records[0]["metadata"]["content_basis"] == "opaque"


def test_windowed_prompt_absent_without_any_user_prompt_logs():
    spans = [span("llm1", name="claude_code.llm_request", start=1_000_000_000, end=1_100_000_000,
                   attributes={"session.id": SESSION, "model": "m", "success": True})]
    records, summary = convert([traces_document(spans)])
    assert records[0]["metadata"]["content_basis"] == "opaque"
    assert summary.sessions_without_interaction_span == 1
