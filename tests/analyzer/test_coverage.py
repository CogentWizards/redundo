from redundo.analyzer.classify import classify_pair
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.lineage import group_by_task
from redundo.analyzer.metrics import build_report
from redundo.analyzer.schema import Event


def make_event(cost_usd=None, task_id_source=None, task_id="t1", step_index=0,
               event_type="tool_call", name="x", content_hash="h", parent_id=None,
               outcome=None):
    metadata = {}
    if task_id_source is not None:
        metadata["task_id_source"] = task_id_source
    return Event(
        task_id=task_id, step_index=step_index, event_type=event_type, name=name,
        content_hash=content_hash, tokens_in=None, tokens_out=None, outcome=outcome,
        timestamp=None, cost_usd=cost_usd, model=None, parent_id=parent_id,
        workflow=None, metadata=metadata,
    )


def coverage(events):
    # No candidate pairs needed -- coverage is measured over the full
    # corpus regardless of what (if anything) got classified.
    return build_report([], events).coverage


def coverage_with_real_classification(events):
    # Runs the actual candidate-pair/classify pipeline, for tests that
    # need real Classification objects rather than an empty list.
    lineages = group_by_task(events)
    pairs = find_candidate_pairs(events)
    classifications = [classify_pair(p, lineages[p.task_id]) for p in pairs]
    return build_report(classifications, events).coverage


def test_all_priced_full_coverage():
    c = coverage([make_event(cost_usd=1.0), make_event(cost_usd=2.5)])
    assert c.total_events == 2
    assert c.priced_events == 2
    assert c.unpriced_events == 0
    assert c.total_priced_cost_usd == 3.5
    assert c.pricing_coverage_fraction == 1.0


def test_mixed_priced_and_unpriced():
    c = coverage([make_event(cost_usd=1.0), make_event(cost_usd=None), make_event(cost_usd=None)])
    assert c.total_events == 3
    assert c.priced_events == 1
    assert c.unpriced_events == 2
    assert c.total_priced_cost_usd == 1.0
    assert round(c.pricing_coverage_fraction, 4) == round(1 / 3, 4)


def test_no_events_gives_zero_fraction_not_a_crash():
    c = coverage([])
    assert c.total_events == 0
    assert c.pricing_coverage_fraction == 0.0
    assert c.task_id_confidence_fraction is None


def test_task_id_source_not_reported_by_any_event_gives_none_not_zero():
    # metadata never carries task_id_source (e.g. a hand-built JSONL, or a
    # source that doesn't set it) -- this must read as "can't be spoken to",
    # not as "0% confident", which would be a fabricated number.
    c = coverage([make_event(cost_usd=1.0), make_event(cost_usd=1.0)])
    assert c.events_with_task_id_source_reported == 0
    assert c.task_id_confidence_fraction is None


def test_task_id_confidence_mixed():
    c = coverage([
        make_event(task_id_source="conversation_id"),
        make_event(task_id_source="conversation_id"),
        make_event(task_id_source="trace_id_fallback"),
    ])
    assert c.events_with_task_id_source_reported == 3
    assert c.events_confident_task_id == 2
    assert c.events_degraded_task_id == 1
    assert round(c.task_id_confidence_fraction, 4) == round(2 / 3, 4)


def test_events_without_task_id_source_dont_count_against_confidence():
    c = coverage([
        make_event(task_id_source="conversation_id"),
        make_event(task_id_source=None),  # not reported -- excluded, not "degraded"
    ])
    assert c.events_with_task_id_source_reported == 1
    assert c.events_confident_task_id == 1
    assert c.task_id_confidence_fraction == 1.0


# --- comparability: tasks with vs. without a candidate pair ---------------

def test_task_with_no_repeats_counts_as_no_candidate_pairs():
    # Two unique tool_calls in one task -- nothing repeats, so zero
    # candidate pairs, even though the task itself was fully loaded.
    events = [
        make_event(task_id="t1", step_index=0, content_hash="a", cost_usd=1.0),
        make_event(task_id="t1", step_index=1, content_hash="b", parent_id=0, cost_usd=2.0),
    ]
    c = coverage_with_real_classification(events)
    assert c.tasks_total == 1
    assert c.tasks_with_candidate_pairs == 0
    assert c.events_in_tasks_with_no_candidate_pairs == 2
    assert c.cost_usd_in_tasks_with_no_candidate_pairs == 3.0
    assert c.tasks_with_candidate_pairs_fraction == 0.0


def test_task_with_a_repeat_counts_as_having_candidate_pairs():
    # Same tool called twice in a row -- one candidate pair.
    events = [
        make_event(task_id="t1", step_index=0, content_hash="a", cost_usd=1.0),
        make_event(task_id="t1", step_index=1, content_hash="a", parent_id=0, cost_usd=1.0,
                    outcome="ok"),
    ]
    c = coverage_with_real_classification(events)
    assert c.tasks_total == 1
    assert c.tasks_with_candidate_pairs == 1
    assert c.events_in_tasks_with_no_candidate_pairs == 0
    assert c.cost_usd_in_tasks_with_no_candidate_pairs == 0.0
    assert c.tasks_with_candidate_pairs_fraction == 1.0


def test_mixed_tasks_split_correctly():
    events = [
        # t1: a repeat -- has a candidate pair.
        make_event(task_id="t1", step_index=0, content_hash="a", cost_usd=1.0),
        make_event(task_id="t1", step_index=1, content_hash="a", parent_id=0, cost_usd=1.0,
                    outcome="ok"),
        # t2: no repeats -- nothing to compare.
        make_event(task_id="t2", step_index=0, content_hash="x", cost_usd=5.0),
        make_event(task_id="t2", step_index=1, content_hash="y", parent_id=0, cost_usd=5.0),
    ]
    c = coverage_with_real_classification(events)
    assert c.tasks_total == 2
    assert c.tasks_with_candidate_pairs == 1
    assert c.tasks_with_candidate_pairs_fraction == 0.5
    assert c.events_in_tasks_with_no_candidate_pairs == 2
    assert c.cost_usd_in_tasks_with_no_candidate_pairs == 10.0
    # Sanity: the task WITH a pair's cost is not double-counted here.
    assert c.total_priced_cost_usd == 12.0


def test_events_in_tasks_with_no_candidate_pairs_ignores_unpriced():
    events = [
        make_event(task_id="t1", step_index=0, content_hash="a", cost_usd=None),
        make_event(task_id="t1", step_index=1, content_hash="b", parent_id=0, cost_usd=None),
    ]
    c = coverage_with_real_classification(events)
    assert c.events_in_tasks_with_no_candidate_pairs == 2
    assert c.cost_usd_in_tasks_with_no_candidate_pairs == 0.0  # nothing priced to sum
