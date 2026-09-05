from redundo.analyzer.lineage import TaskLineage
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                parent_id=None, outcome=None, task_id="t1"):
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
        metadata={},
    )


def test_linear_fallback_when_no_parent_id():
    events = [make_event(0), make_event(1), make_event(2)]
    lineage = TaskLineage.build(events)

    assert lineage.parent_of(events[1]).step_index == 0
    assert lineage.parent_of(events[2]).step_index == 1
    assert lineage.parent_of(events[0]) is None
    assert [e.step_index for e in lineage.ancestors_of(events[2])] == [1, 0]


def test_explicit_parent_id_overrides_linear_fallback():
    # step 2's real parent is step 0, not step 1 -- a branch.
    events = [make_event(0), make_event(1), make_event(2, parent_id=0)]
    lineage = TaskLineage.build(events)

    assert lineage.parent_of(events[2]).step_index == 0
    assert [e.step_index for e in lineage.ancestors_of(events[2])] == [0]


def test_sibling_branches_do_not_see_each_other():
    root = make_event(0)
    branch_a = make_event(1, parent_id=0)
    branch_b = make_event(2, parent_id=0)
    lineage = TaskLineage.build([root, branch_a, branch_b])

    ancestors_of_b = [e.step_index for e in lineage.ancestors_of(branch_b)]
    assert ancestors_of_b == [0]
    assert 1 not in ancestors_of_b  # branch_a is not an ancestor of branch_b


def test_children_of():
    root = make_event(0)
    child = make_event(1, parent_id=0)
    lineage = TaskLineage.build([root, child])
    assert [e.step_index for e in lineage.children_of(root)] == [1]


def test_terminal_outcome_is_last_event_by_step_index():
    events = [make_event(0, outcome="error"), make_event(1, outcome="ok")]
    lineage = TaskLineage.build(events)
    assert lineage.terminal_outcome == "ok"


def test_terminal_outcome_none_when_last_event_has_none():
    events = [make_event(0, outcome="ok"), make_event(1, outcome=None)]
    lineage = TaskLineage.build(events)
    assert lineage.terminal_outcome is None


def test_path_between():
    events = [make_event(0), make_event(1), make_event(2), make_event(3)]
    lineage = TaskLineage.build(events)
    between = lineage.path_between(events[0], events[3])
    assert [e.step_index for e in between] == [2, 1]
