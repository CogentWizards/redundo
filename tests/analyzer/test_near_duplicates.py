from redundo.analyzer.near_duplicates import find_near_duplicate_pairs
from redundo.analyzer.schema import Event

ZERO_FP = "0" * 16
# XOR against ZERO_FP: each hex 'f' contributes 4 set bits.
CLOSE_FP = "f" * 2 + "0" * 14  # distance 8 from ZERO_FP
FAR_FP = "f" * 8 + "0" * 8  # distance 32 from ZERO_FP
ONE_BIT_FP = "0" * 15 + "1"  # distance 1 from ZERO_FP


def make_event(
    step_index,
    event_type="tool_call",
    name="search",
    content_hash="h1",
    parent_id=None,
    task_id="t1",
    fingerprint=None,
):
    metadata = {}
    if fingerprint is not None:
        metadata["similarity_fingerprint"] = fingerprint
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
        metadata=metadata,
    )


def test_near_identical_calls_form_a_near_duplicate_pair():
    events = [
        make_event(0, content_hash="h1", fingerprint=ZERO_FP),
        make_event(1, content_hash="h2", fingerprint=CLOSE_FP),
    ]
    pairs = find_near_duplicate_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.step_index == 0
    assert pairs[0].repeat.step_index == 1
    assert pairs[0].hamming_distance == 8


def test_distance_above_threshold_is_not_a_pair():
    events = [
        make_event(0, content_hash="h1", fingerprint=ZERO_FP),
        make_event(1, content_hash="h2", fingerprint=FAR_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_exact_content_hash_match_never_appears_here_even_within_threshold():
    # Identical content_hash -> identical fingerprint (distance 0, well
    # within any reasonable threshold) -- but this relationship already
    # belongs to cycles.py's exact-match pipeline. It must never also
    # surface as a near-duplicate pair.
    events = [
        make_event(0, content_hash="same", fingerprint=ZERO_FP),
        make_event(1, content_hash="same", fingerprint=ZERO_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_missing_fingerprint_on_repeat_degrades_to_no_pair():
    events = [
        make_event(0, content_hash="h1", fingerprint=ZERO_FP),
        make_event(1, content_hash="h2", fingerprint=None),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_missing_fingerprint_on_ancestor_degrades_to_no_pair():
    events = [
        make_event(0, content_hash="h1", fingerprint=None),
        make_event(1, content_hash="h2", fingerprint=CLOSE_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_different_name_is_not_a_near_duplicate():
    events = [
        make_event(0, name="search", content_hash="h1", fingerprint=ZERO_FP),
        make_event(1, name="fetch", content_hash="h2", fingerprint=CLOSE_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_different_event_type_is_not_a_near_duplicate():
    events = [
        make_event(0, event_type="tool_call", content_hash="h1", fingerprint=ZERO_FP),
        make_event(1, event_type="llm_call", content_hash="h2", fingerprint=CLOSE_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_sibling_branches_are_not_a_near_duplicate():
    root = make_event(0, name="root_call", content_hash="root", fingerprint=ZERO_FP)
    branch_a = make_event(1, parent_id=0, content_hash="a", fingerprint=CLOSE_FP)
    branch_b = make_event(2, parent_id=0, content_hash="b", fingerprint=CLOSE_FP)
    assert find_near_duplicate_pairs([root, branch_a, branch_b]) == []


def test_nearest_ancestor_wins_not_a_farther_more_similar_one():
    # A chain of three, all pairwise within threshold: the nearest-only
    # rule (mirroring cycles.py) means step 2 pairs with step 1, not step 0,
    # even though every pair here is "similar."
    events = [
        make_event(0, content_hash="h0", fingerprint=ZERO_FP),
        make_event(1, content_hash="h1", fingerprint=ONE_BIT_FP),
        make_event(2, content_hash="h2", fingerprint=CLOSE_FP),
    ]
    pairs = find_near_duplicate_pairs(events)
    assert len(pairs) == 2
    assert (pairs[0].original.step_index, pairs[0].repeat.step_index) == (0, 1)
    assert (pairs[1].original.step_index, pairs[1].repeat.step_index) == (1, 2)


def test_tool_result_never_becomes_a_near_duplicate_repeat():
    call = make_event(0, event_type="tool_call", content_hash="h1", fingerprint=ZERO_FP)
    result_a = make_event(
        1, event_type="tool_result", content_hash="r1", fingerprint=ZERO_FP
    )
    result_b = make_event(
        2, event_type="tool_result", content_hash="r2", fingerprint=CLOSE_FP
    )
    assert find_near_duplicate_pairs([call, result_a, result_b]) == []


def test_different_tasks_are_independent():
    events = [
        make_event(0, task_id="t1", content_hash="h1", fingerprint=ZERO_FP),
        make_event(0, task_id="t2", content_hash="h2", fingerprint=CLOSE_FP),
    ]
    assert find_near_duplicate_pairs(events) == []


def test_intervening_events_captured_in_order():
    original = make_event(0, content_hash="h1", fingerprint=ZERO_FP)
    # Different name from original/repeat (so they can never pair with
    # them) and far apart from *each other* too (so they don't form their
    # own near-duplicate pair and pollute the count this test checks).
    mid1 = make_event(1, name="other_tool", content_hash="mid1", fingerprint=ZERO_FP)
    mid2 = make_event(2, name="other_tool", content_hash="mid2", fingerprint=FAR_FP)
    repeat = make_event(3, content_hash="h2", fingerprint=CLOSE_FP)
    pairs = find_near_duplicate_pairs([original, mid1, mid2, repeat])
    assert len(pairs) == 1
    assert [e.step_index for e in pairs[0].intervening] == [1, 2]
