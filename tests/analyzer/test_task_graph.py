from redundo.analyzer.schema import Event
from redundo.analyzer.task_graph import build_task_graph, same_component


def make_event(task_id, step_index=0, parent_task_id=None, link_kind=None):
    metadata = {}
    if parent_task_id is not None:
        metadata["parent_task_id"] = parent_task_id
    if link_kind is not None:
        metadata["parent_task_link_kind"] = link_kind
    return Event(
        task_id=task_id, step_index=step_index, event_type="tool_call", name="x",
        content_hash="h", tokens_in=None, tokens_out=None, outcome=None,
        timestamp=None, cost_usd=None, model=None, parent_id=None,
        workflow=None, metadata=metadata,
    )


def test_two_tasks_with_no_link_are_different_components():
    events = [make_event("t1"), make_event("t2")]
    graph = build_task_graph(events)
    assert graph.parent_of == {"t1": None, "t2": None}
    assert not same_component(graph, "t1", "t2")


def test_direct_parent_link_puts_both_tasks_in_the_same_component():
    events = [make_event("parent"), make_event("child", parent_task_id="parent")]
    graph = build_task_graph(events)
    assert graph.parent_of["child"] == "parent"
    assert same_component(graph, "parent", "child")


def test_transitive_chain_puts_all_three_in_the_same_component():
    events = [
        make_event("grandparent"),
        make_event("parent", parent_task_id="grandparent"),
        make_event("child", parent_task_id="parent"),
    ]
    graph = build_task_graph(events)
    assert same_component(graph, "grandparent", "child")
    assert same_component(graph, "grandparent", "parent")


def test_parent_task_never_seen_as_its_own_task_still_merges_the_edge():
    # The parent task's own events weren't captured (a different source,
    # or simply never adapted). The edge from the child side is still
    # real and should still connect anything else that references the
    # same parent_task_id.
    events = [
        make_event("child-a", parent_task_id="missing-parent"),
        make_event("child-b", parent_task_id="missing-parent"),
    ]
    graph = build_task_graph(events)
    assert same_component(graph, "child-a", "child-b")


def test_conflicting_parent_task_id_across_a_tasks_own_events_is_treated_as_absent():
    events = [
        make_event("t1", step_index=0, parent_task_id="a"),
        make_event("t1", step_index=1, parent_task_id="b"),
        make_event("a"),
        make_event("b"),
    ]
    graph = build_task_graph(events)
    assert graph.parent_of["t1"] is None
    assert not same_component(graph, "t1", "a")
    assert not same_component(graph, "t1", "b")


def test_unrelated_task_never_mentioned_is_never_considered_same_component():
    events = [make_event("t1"), make_event("t2", parent_task_id="t1")]
    graph = build_task_graph(events)
    assert not same_component(graph, "t2", "t3")


# --- continuation_parent_of: a strict subset of parent_of -----------------

def test_delegation_link_never_appears_in_continuation_parent_of():
    events = [
        make_event("parent"),
        make_event("child", parent_task_id="parent", link_kind="delegation"),
    ]
    graph = build_task_graph(events)
    assert graph.parent_of["child"] == "parent"  # still a real, related edge
    assert graph.continuation_parent_of.get("child") is None


def test_unmarked_link_never_appears_in_continuation_parent_of():
    # No link_kind at all -- e.g. hermes-otel's real parent_task_id today,
    # which never sets one. Absence must not be treated as continuation.
    events = [
        make_event("parent"),
        make_event("child", parent_task_id="parent"),
    ]
    graph = build_task_graph(events)
    assert graph.parent_of["child"] == "parent"
    assert graph.continuation_parent_of.get("child") is None


def test_continuation_link_appears_in_both_parent_of_and_continuation_parent_of():
    events = [
        make_event("session-1"),
        make_event("session-2", parent_task_id="session-1", link_kind="continuation"),
    ]
    graph = build_task_graph(events)
    assert graph.parent_of["session-2"] == "session-1"
    assert graph.continuation_parent_of["session-2"] == "session-1"


def test_continuation_chain_of_three():
    events = [
        make_event("session-1"),
        make_event("session-2", parent_task_id="session-1", link_kind="continuation"),
        make_event("session-3", parent_task_id="session-2", link_kind="continuation"),
    ]
    graph = build_task_graph(events)
    assert graph.continuation_parent_of["session-2"] == "session-1"
    assert graph.continuation_parent_of["session-3"] == "session-2"
