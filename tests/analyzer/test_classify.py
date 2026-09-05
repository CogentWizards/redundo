from redundo.analyzer.classify import Signal, Verdict, classify_pair
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.lineage import group_by_task
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                parent_id=None, outcome=None, metadata=None, task_id="t1"):
    return Event(
        task_id=task_id,
        step_index=step_index,
        event_type=event_type,
        name=name,
        content_hash=content_hash,
        tokens_in=None,
        tokens_out=None,
        outcome=outcome,
        timestamp=None,
        cost_usd=None,
        model=None,
        parent_id=parent_id,
        workflow=None,
        metadata=metadata or {},
    )


def classify_first_pair(events):
    lineage = group_by_task(events)["t1"]
    pairs = find_candidate_pairs(events)
    assert len(pairs) == 1, f"expected exactly one candidate pair, got {len(pairs)}"
    return classify_pair(pairs[0], lineage)


def test_confirmed_waste_unreachable_without_a_captured_result():
    # Safety property, not just a spot-check: whatever the write status or
    # terminal outcome, CONFIRMED_WASTE must never fire when no tool_result
    # was ever captured for the call -- i.e. when result identity is
    # structurally unknowable, not merely unconfirmed. This is the case
    # every source without result-hash capture (or one that's had it
    # stripped) hits on every single pair, so it has to hold universally,
    # not just for the specific fixtures this was checked against.
    for write_metadata, outcome in [
        (None, "error"),  # write unknown, terminal failure
        ({"write": False}, "error"),  # write confirmed absent, terminal failure
        (None, None),  # everything unknown
        ({"write": False}, "ok"),  # write confirmed absent, terminal success
    ]:
        events = [
            make_event(0, event_type="tool_call"),
            # no tool_result for step 0 -- result is structurally unobservable
            make_event(1, name="unrelated_tool", content_hash="unrelated",
                       metadata=write_metadata),
            make_event(2, event_type="tool_call"),
            make_event(3, event_type="tool_result", content_hash="whatever", outcome=outcome),
        ]
        classification = classify_first_pair(events)
        assert classification.result_signal == Signal.UNKNOWN
        assert classification.verdict != Verdict.CONFIRMED_WASTE, (
            f"CONFIRMED_WASTE fired with no captured result "
            f"(write_metadata={write_metadata}, outcome={outcome})"
        )


def test_confirmed_waste_identical_result_no_write_terminal_failure():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, event_type="tool_call"),
        make_event(3, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.CONFIRMED_WASTE
    assert classification.result_signal == Signal.WASTE_SUPPORTING
    assert classification.write_signal == Signal.WASTE_SUPPORTING
    assert classification.terminal_signal == Signal.WASTE_SUPPORTING


def test_likely_legitimate_when_result_changed():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="result_a"),
        make_event(2, event_type="tool_call"),
        make_event(3, event_type="tool_result", content_hash="result_b", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.LIKELY_LEGITIMATE
    assert classification.result_signal == Signal.LEGIT_SUPPORTING


def test_likely_legitimate_when_write_intervenes():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, name="unrelated_tool", content_hash="unrelated", metadata={"write": True}),
        make_event(3, event_type="tool_call"),
        make_event(4, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.LIKELY_LEGITIMATE
    assert classification.write_signal == Signal.LEGIT_SUPPORTING


def test_unclassified_when_terminal_success_but_write_confirmed_and_result_unknown():
    # No tool_result at all (result unknown), no intervening events. "No
    # intervening write" is still a confirmed waste-supporting signal here
    # -- there was nothing between the two calls to even check, but
    # _write_signal() reports that the same way it would report an
    # explicitly-verified no-write elsewhere, and this call-level signal
    # must block terminal success the same way an explicit one would.
    # Otherwise a source with no result-correlation data at all (or one
    # with result_hash stripped) would flip straight to likely_legitimate
    # on task success alone, rather than degrading to unclassified.
    events = [
        make_event(0, event_type="tool_call", outcome=None),
        make_event(1, event_type="tool_call", outcome="ok"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.UNCLASSIFIED
    assert classification.terminal_signal == Signal.LEGIT_SUPPORTING
    assert classification.result_signal == Signal.UNKNOWN
    assert classification.write_signal == Signal.WASTE_SUPPORTING


def test_likely_legitimate_when_terminal_success_and_both_other_signals_unknown():
    # Genuinely nothing else is known: an intervening call with no write
    # metadata at all makes write_signal UNKNOWN too, not just result. With
    # neither call-level signal confirmed waste, terminal success is the
    # only signal available and is allowed to stand on its own.
    events = [
        make_event(0, event_type="tool_call", outcome=None),
        make_event(1, name="mystery_tool", content_hash="mystery"),
        make_event(2, event_type="tool_call", outcome="ok"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.LIKELY_LEGITIMATE
    assert classification.terminal_signal == Signal.LEGIT_SUPPORTING
    assert classification.result_signal == Signal.UNKNOWN
    assert classification.write_signal == Signal.UNKNOWN


def test_unclassified_when_terminal_success_but_call_confirms_waste():
    # Identical result, no intervening write -- both call-level signals
    # confirm waste on their own. The task succeeding anyway isn't proof
    # this specific repeat contributed, so terminal success must not
    # override two confirmed waste-supporting signals.
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, event_type="tool_call"),
        make_event(3, event_type="tool_result", content_hash="same_result", outcome="ok"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.UNCLASSIFIED
    assert classification.result_signal == Signal.WASTE_SUPPORTING
    assert classification.write_signal == Signal.WASTE_SUPPORTING
    assert classification.terminal_signal == Signal.LEGIT_SUPPORTING


def test_unclassified_when_result_unknown_and_terminal_unknown():
    events = [
        make_event(0, event_type="tool_call", outcome=None),
        make_event(1, event_type="tool_call", outcome=None),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.UNCLASSIFIED
    assert classification.result_signal == Signal.UNKNOWN
    assert classification.terminal_signal == Signal.UNKNOWN


def test_unclassified_when_write_status_unrecorded_and_no_other_legit_signal():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        # intervening event with no write metadata at all -- genuinely unknown,
        # not assumed False.
        make_event(2, name="mystery_tool", content_hash="mystery"),
        make_event(3, event_type="tool_call"),
        make_event(4, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.UNCLASSIFIED
    assert classification.write_signal == Signal.UNKNOWN


def test_intervening_llm_call_with_no_write_flag_does_not_block_confirmed_waste():
    # An llm_call between the two repeated tool calls -- the normal shape
    # of a ReAct loop -- has no write metadata at all, same as any real
    # OpenInference source that never annotates it. llm_call is
    # structurally incapable of a side effect (a pure completion step),
    # so its absence must not read as "unknown" the way a tool_call's
    # would; otherwise confirmed_waste becomes unreachable on any trace
    # with an LLM turn between repeats -- which is nearly all of them.
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, event_type="llm_call", name="gpt-5.6"),
        make_event(3, event_type="tool_call"),
        make_event(4, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.CONFIRMED_WASTE
    assert classification.write_signal == Signal.WASTE_SUPPORTING


def test_intervening_llm_call_with_explicit_write_true_still_blocks():
    # A source that does annotate an llm_call's write flag is still
    # respected -- the type-based default only fills in when unset.
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, event_type="llm_call", name="gpt-5.6", metadata={"write": True}),
        make_event(3, event_type="tool_call"),
        make_event(4, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.LIKELY_LEGITIMATE
    assert classification.write_signal == Signal.LEGIT_SUPPORTING


def test_explicit_write_false_does_not_block_confirmed_waste():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same_result"),
        make_event(2, name="read_only_tool", content_hash="ro", metadata={"write": False}),
        make_event(3, event_type="tool_call"),
        make_event(4, event_type="tool_result", content_hash="same_result", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.verdict == Verdict.CONFIRMED_WASTE


def test_llm_call_without_response_hash_cannot_reach_confirmed_waste():
    # No llm_result event type exists; without metadata.response_hash on
    # both calls, result identity is structurally unknowable.
    events = [
        make_event(0, event_type="llm_call", name="gpt-5.6", outcome=None),
        make_event(1, event_type="llm_call", name="gpt-5.6", outcome="error"),
    ]
    classification = classify_first_pair(events)
    assert classification.result_signal == Signal.UNKNOWN
    assert classification.verdict == Verdict.UNCLASSIFIED


def test_llm_call_with_response_hash_can_reach_confirmed_waste():
    events = [
        make_event(0, event_type="llm_call", name="gpt-5.6", metadata={"response_hash": "r1"}),
        make_event(
            1, event_type="llm_call", name="gpt-5.6", outcome="error",
            metadata={"response_hash": "r1"},
        ),
    ]
    classification = classify_first_pair(events)
    assert classification.result_signal == Signal.WASTE_SUPPORTING
    assert classification.verdict == Verdict.CONFIRMED_WASTE
