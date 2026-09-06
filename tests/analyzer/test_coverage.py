from redundo.analyzer.metrics import compute_generic_coverage
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
    return compute_generic_coverage(events)


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


def test_extra_notes_starts_empty():
    # Analysis-specific coverage caveats (e.g. WasteAnalysis's candidate-pair
    # comparability note) are appended by the analysis itself, not computed
    # here -- compute_generic_coverage() never populates this.
    c = coverage([make_event(cost_usd=1.0)])
    assert c.extra_notes == []
