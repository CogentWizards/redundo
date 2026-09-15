from redundo.analyzer.context_drift import (
    _align,
    find_continuation_chains,
    find_drift_hints,
)
from redundo.analyzer.schema import Event
from redundo.analyzer.task_graph import build_task_graph

ZERO_FP = "0" * 16
CLOSE_FP = "f" * 2 + "0" * 14  # distance 8 from ZERO_FP
FAR_FP = "f" * 8 + "0" * 8  # distance 32 from ZERO_FP


def make_event(
    task_id, step_index, event_type="tool_call", name="search", fingerprint=None,
    parent_task_id=None, link_kind=None,
):
    metadata = {}
    if fingerprint is not None:
        metadata["similarity_fingerprint"] = fingerprint
    if parent_task_id is not None:
        metadata["parent_task_id"] = parent_task_id
    if link_kind is not None:
        metadata["parent_task_link_kind"] = link_kind
    return Event(
        task_id=task_id, step_index=step_index, event_type=event_type, name=name,
        content_hash="h", tokens_in=None, tokens_out=None, outcome=None,
        timestamp=None, cost_usd=None, model=None, parent_id=None,
        workflow=None, metadata=metadata,
    )


# --- _align: the alignment primitive, tested in isolation -----------------

def test_identical_sequences_align_with_zero_distance():
    seq = [
        make_event("t", 0, name="a"),
        make_event("t", 1, name="b"),
    ]
    distance, pairs = _align(seq, seq)
    assert distance == 0
    assert pairs == [(0, 0), (1, 1)]


def test_pure_insertion_costs_one_per_added_call():
    a = [make_event("t", 0, name="x")]
    b = [make_event("t", 0, name="x"), make_event("t", 1, name="y")]
    distance, pairs = _align(a, b)
    assert distance == 1
    assert pairs == [(0, 0), (None, 1)]


def test_pure_deletion_costs_one_per_removed_call():
    a = [make_event("t", 0, name="x"), make_event("t", 1, name="y")]
    b = [make_event("t", 0, name="x")]
    distance, pairs = _align(a, b)
    assert distance == 1
    assert pairs == [(0, 0), (1, None)]


def test_substitution_aligns_positionally_with_differing_signature():
    a = [make_event("t", 0, name="x")]
    b = [make_event("t", 0, name="y")]
    distance, pairs = _align(a, b)
    assert distance == 1
    assert pairs == [(0, 0)]


def test_completely_disjoint_sequences_cost_the_longer_length():
    a = [make_event("t", 0, name="x"), make_event("t", 1, name="y")]
    b = [make_event("t", 0, name="p"), make_event("t", 1, name="q"), make_event("t", 2, name="r")]
    distance, _ = _align(a, b)
    assert distance == 3  # 2 substitutions + 1 insertion


def test_empty_sequences_align_trivially():
    assert _align([], []) == (0, [])
    distance, pairs = _align([], [make_event("t", 0, name="x")])
    assert distance == 1
    assert pairs == [(None, 0)]


# --- find_continuation_chains ----------------------------------------------

def test_no_continuation_edges_produces_no_chains():
    events = [make_event("t1", 0), make_event("t2", 0)]
    graph = build_task_graph(events)
    assert find_continuation_chains(graph) == []


def test_delegation_edge_alone_produces_no_chain():
    events = [
        make_event("parent", 0),
        make_event("child", 0, parent_task_id="parent", link_kind="delegation"),
    ]
    graph = build_task_graph(events)
    assert find_continuation_chains(graph) == []


def test_two_task_continuation_chain():
    events = [
        make_event("s1", 0),
        make_event("s2", 0, parent_task_id="s1", link_kind="continuation"),
    ]
    graph = build_task_graph(events)
    assert find_continuation_chains(graph) == [["s1", "s2"]]


def test_three_task_continuation_chain_in_root_to_latest_order():
    events = [
        make_event("s1", 0),
        make_event("s2", 0, parent_task_id="s1", link_kind="continuation"),
        make_event("s3", 0, parent_task_id="s2", link_kind="continuation"),
    ]
    graph = build_task_graph(events)
    assert find_continuation_chains(graph) == [["s1", "s2", "s3"]]


def test_two_independent_chains_both_found():
    events = [
        make_event("a1", 0),
        make_event("a2", 0, parent_task_id="a1", link_kind="continuation"),
        make_event("b1", 0),
        make_event("b2", 0, parent_task_id="b1", link_kind="continuation"),
    ]
    graph = build_task_graph(events)
    chains = {tuple(c) for c in find_continuation_chains(graph)}
    assert chains == {("a1", "a2"), ("b1", "b2")}


# --- find_drift_hints: end-to-end ------------------------------------------

def test_no_hints_for_a_single_task_chain():
    events = [make_event("t1", 0)]
    assert find_drift_hints(events) == []


def test_identical_hop_reports_zero_shape_and_content_distance():
    events = [
        make_event("s1", 0, event_type="llm_call", name="model", fingerprint=ZERO_FP),
        make_event(
            "s2", 0, event_type="llm_call", name="model", fingerprint=ZERO_FP,
            parent_task_id="s1", link_kind="continuation",
        ),
    ]
    hints = find_drift_hints(events)
    assert len(hints) == 1
    hint = hints[0]
    assert hint.predecessor_task_id == "s1"
    assert hint.successor_task_id == "s2"
    assert hint.shape_distance == 0
    assert hint.content_distances == ((0, 0, 0),)
    assert hint.added == ()
    assert hint.removed == ()
    assert hint.substituted == ()


def test_diverging_content_at_a_matched_step_is_reported():
    events = [
        make_event("s1", 0, event_type="llm_call", name="model", fingerprint=ZERO_FP),
        make_event(
            "s2", 0, event_type="llm_call", name="model", fingerprint=FAR_FP,
            parent_task_id="s1", link_kind="continuation",
        ),
    ]
    hints = find_drift_hints(events)
    assert hints[0].content_distances == ((0, 0, 32),)


def test_added_call_reported_by_name_and_step():
    events = [
        make_event("s1", 0, name="search"),
        make_event("s2", 0, name="search", parent_task_id="s1", link_kind="continuation"),
        make_event("s2", 1, name="retry_search", parent_task_id="s1", link_kind="continuation"),
    ]
    hints = find_drift_hints(events)
    assert hints[0].added == (("tool_call", "retry_search", 1),)
    assert hints[0].removed == ()


def test_removed_call_reported_by_name_and_step():
    events = [
        make_event("s1", 0, name="search"),
        make_event("s1", 1, name="verify"),
        make_event("s2", 0, name="search", parent_task_id="s1", link_kind="continuation"),
    ]
    hints = find_drift_hints(events)
    assert hints[0].removed == (("tool_call", "verify", 1),)
    assert hints[0].added == ()


def test_substituted_call_reported_as_a_pair_not_added_and_removed():
    events = [
        make_event("s1", 0, name="search"),
        make_event("s2", 0, name="read_file", parent_task_id="s1", link_kind="continuation"),
    ]
    hints = find_drift_hints(events)
    hint = hints[0]
    assert hint.substituted == ((("tool_call", "search", 0), ("tool_call", "read_file", 0)),)
    assert hint.added == ()
    assert hint.removed == ()


def test_substitution_never_produces_a_content_distance():
    # A tool_call replaced by a differently-named tool_call: comparing
    # fingerprints across that boundary would not be a meaningful signal,
    # the same restriction near_duplicates.py applies within one task.
    events = [
        make_event("s1", 0, name="search", fingerprint=ZERO_FP),
        make_event(
            "s2", 0, name="read_file", fingerprint=ZERO_FP,
            parent_task_id="s1", link_kind="continuation",
        ),
    ]
    hints = find_drift_hints(events)
    assert hints[0].content_distances == ()


def test_missing_fingerprint_on_one_side_excludes_that_step_from_content_distances():
    events = [
        make_event("s1", 0, name="model", event_type="llm_call", fingerprint=None),
        make_event(
            "s2", 0, name="model", event_type="llm_call", fingerprint=ZERO_FP,
            parent_task_id="s1", link_kind="continuation",
        ),
    ]
    hints = find_drift_hints(events)
    assert hints[0].content_distances == ()


def test_three_task_chain_produces_two_hops():
    events = [
        make_event("s1", 0, name="a"),
        make_event("s2", 0, name="a", parent_task_id="s1", link_kind="continuation"),
        make_event("s3", 0, name="a", parent_task_id="s2", link_kind="continuation"),
    ]
    hints = find_drift_hints(events)
    assert [(h.predecessor_task_id, h.successor_task_id) for h in hints] == [
        ("s1", "s2"), ("s2", "s3"),
    ]


def test_tool_result_rows_never_participate_in_the_call_sequence():
    events = [
        make_event("s1", 0, event_type="tool_call", name="search"),
        make_event("s1", 1, event_type="tool_result", name="search"),
        make_event(
            "s2", 0, event_type="tool_call", name="search",
            parent_task_id="s1", link_kind="continuation",
        ),
    ]
    hints = find_drift_hints(events)
    assert hints[0].predecessor_call_count == 1  # the tool_result is excluded
