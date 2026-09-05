from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                parent_id=None, task_id="t1"):
    return Event(
        task_id=task_id,
        step_index=step_index,
        event_type=event_type,
        name=name,
        content_hash=content_hash,
        tokens_in=None,
        tokens_out=None,
        outcome=None,
        timestamp=None,
        cost_usd=None,
        model=None,
        parent_id=parent_id,
        workflow=None,
        metadata={},
    )


def test_two_identical_calls_form_one_pair():
    events = [make_event(0), make_event(1)]
    pairs = find_candidate_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.step_index == 0
    assert pairs[0].repeat.step_index == 1
    assert pairs[0].intervening == ()


def test_three_call_chain_forms_two_adjacent_pairs_not_three():
    events = [make_event(0), make_event(1), make_event(2)]
    pairs = find_candidate_pairs(events)
    assert len(pairs) == 2
    assert (pairs[0].original.step_index, pairs[0].repeat.step_index) == (0, 1)
    assert (pairs[1].original.step_index, pairs[1].repeat.step_index) == (1, 2)


def test_different_content_hash_is_not_a_pair():
    events = [make_event(0, content_hash="h1"), make_event(1, content_hash="h2")]
    assert find_candidate_pairs(events) == []


def test_different_name_is_not_a_pair():
    events = [make_event(0, name="search"), make_event(1, name="fetch")]
    assert find_candidate_pairs(events) == []


def test_sibling_branches_are_not_a_pair():
    root = make_event(0, name="root_call", content_hash="root")
    branch_a = make_event(1, parent_id=0)
    branch_b = make_event(2, parent_id=0)
    pairs = find_candidate_pairs([root, branch_a, branch_b])
    assert pairs == []


def test_intervening_events_captured_in_order():
    original = make_event(0)
    mid1 = make_event(1, name="other_tool", content_hash="mid1")
    mid2 = make_event(2, name="other_tool", content_hash="mid2")
    repeat = make_event(3)
    pairs = find_candidate_pairs([original, mid1, mid2, repeat])
    assert len(pairs) == 1
    assert [e.step_index for e in pairs[0].intervening] == [1, 2]


def test_tool_result_never_becomes_a_repeat():
    call = make_event(0, event_type="tool_call")
    result_a = make_event(1, event_type="tool_result", content_hash="same_result")
    result_b = make_event(2, event_type="tool_result", content_hash="same_result")
    pairs = find_candidate_pairs([call, result_a, result_b])
    assert pairs == []


def test_llm_call_repeats_are_detected():
    events = [
        make_event(0, event_type="llm_call", name="gpt-5.6", content_hash="prompt_hash_1"),
        make_event(1, event_type="llm_call", name="gpt-5.6", content_hash="prompt_hash_1"),
    ]
    pairs = find_candidate_pairs(events)
    assert len(pairs) == 1


def test_different_tasks_are_independent():
    events = [
        make_event(0, task_id="t1"),
        make_event(0, task_id="t2"),
    ]
    assert find_candidate_pairs(events) == []
