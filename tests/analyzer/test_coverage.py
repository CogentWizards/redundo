from redundo.analyzer.metrics import (
    COST_BASIS_DIRECT,
    COST_BASIS_SYNTHESIZED,
    compute_generic_coverage,
)
from redundo.analyzer.schema import Event


def make_event(cost_usd=None, task_id_source=None, task_id="t1", step_index=0,
               event_type="tool_call", name="x", content_hash="h", parent_id=None,
               outcome=None, cost_basis=None, source_path=None):
    metadata = {}
    if task_id_source is not None:
        metadata["task_id_source"] = task_id_source
    if cost_basis is not None:
        metadata["cost_basis"] = cost_basis
    if source_path is not None:
        metadata["source_path"] = source_path
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


def _synthesized_event(cost_usd):
    return Event(
        task_id="t1", step_index=0, event_type="llm_call", name="x",
        content_hash="h", tokens_in=None, tokens_out=None, outcome=None,
        timestamp=None, cost_usd=cost_usd, model=None, parent_id=None,
        workflow=None, metadata={"synthesized_cost_only": True},
    )


def test_synthesized_cost_only_events_tracked_separately():
    c = coverage([_synthesized_event(0.5), make_event(cost_usd=1.0)])
    assert c.synthesized_cost_only_events == 1
    assert c.synthesized_cost_only_usd == 0.5
    # Still counted in the ordinary totals too -- this is additive
    # tracking, not a separate, exclusive bucket.
    assert c.priced_events == 2
    assert c.total_priced_cost_usd == 1.5


def test_synthesized_cost_only_with_no_cost_still_counts_the_event_not_the_dollars():
    c = coverage([_synthesized_event(None)])
    assert c.synthesized_cost_only_events == 1
    assert c.synthesized_cost_only_usd == 0.0


def test_ordinary_events_never_count_as_synthesized_cost_only():
    c = coverage([make_event(cost_usd=1.0)])
    assert c.synthesized_cost_only_events == 0
    assert c.synthesized_cost_only_usd == 0.0


def test_total_call_events_counts_llm_and_tool_calls_not_results():
    c = coverage([
        make_event(event_type="llm_call"),
        make_event(event_type="tool_call"),
        make_event(event_type="tool_result"),
    ])
    assert c.total_call_events == 2


def test_cost_basis_absent_is_reported_as_direct_from_source():
    c = coverage([make_event(cost_usd=1.0), make_event(cost_usd=2.0)])
    assert set(c.cost_by_basis) == {COST_BASIS_DIRECT}
    assert c.cost_by_basis[COST_BASIS_DIRECT].events == 2
    assert c.cost_by_basis[COST_BASIS_DIRECT].usd == 3.0


def test_cost_basis_mixture_is_broken_out_separately():
    c = coverage([
        make_event(cost_usd=1.0),
        make_event(cost_usd=2.0, cost_basis="estimated_from_bundled_pricing_table"),
        make_event(cost_usd=0.5, cost_basis="estimated_from_bundled_pricing_table"),
        make_event(cost_usd=4.0, cost_basis="apportioned_from_metrics_by_tokens"),
    ])
    assert c.cost_by_basis[COST_BASIS_DIRECT].events == 1
    assert c.cost_by_basis[COST_BASIS_DIRECT].usd == 1.0
    assert c.cost_by_basis["estimated_from_bundled_pricing_table"].events == 2
    assert c.cost_by_basis["estimated_from_bundled_pricing_table"].usd == 2.5
    assert c.cost_by_basis["apportioned_from_metrics_by_tokens"].events == 1
    assert c.cost_by_basis["apportioned_from_metrics_by_tokens"].usd == 4.0
    # sums back to the ordinary totals -- additive tracking, not a
    # separate, exclusive view of the data.
    assert c.priced_events == 4
    assert c.total_priced_cost_usd == 7.5


def test_synthesized_cost_only_wins_over_an_unrelated_cost_basis_value():
    # Shouldn't happen in practice (an adapter wouldn't set both), but the
    # synthesized flag is a stronger, more specific claim and must win.
    events = [
        Event(
            task_id="t1", step_index=0, event_type="llm_call", name="x",
            content_hash="h", tokens_in=None, tokens_out=None, outcome=None,
            timestamp=None, cost_usd=1.0, model=None, parent_id=None,
            workflow=None,
            metadata={
                "synthesized_cost_only": True,
                "cost_basis": "estimated_from_bundled_pricing_table",
            },
        ),
    ]
    c = coverage(events)
    assert set(c.cost_by_basis) == {COST_BASIS_SYNTHESIZED}
    assert c.cost_by_basis[COST_BASIS_SYNTHESIZED].usd == 1.0


def test_unpriced_events_dont_appear_in_cost_by_basis():
    c = coverage([make_event(cost_usd=None)])
    assert c.cost_by_basis == {}


# --- source_path --------------------------------------------------------

def test_source_path_reported_when_every_event_agrees():
    c = coverage([
        make_event(source_path="/tmp/otlp_traces"),
        make_event(source_path="/tmp/otlp_traces"),
    ])
    assert c.source_path == "/tmp/otlp_traces"


def test_source_path_none_when_absent():
    c = coverage([make_event()])
    assert c.source_path is None


def test_source_path_none_when_events_disagree():
    # A corpus hand-assembled from more than one adapt run -- never guess
    # which one to show.
    c = coverage([
        make_event(source_path="/tmp/otlp_traces_a"),
        make_event(source_path="/tmp/otlp_traces_b"),
    ])
    assert c.source_path is None


# --- synthesized_cost_only_samples ---------------------------------------

def _synthesized_event_with_position(task_id, step_index):
    return Event(
        task_id=task_id, step_index=step_index, event_type="llm_call", name="x",
        content_hash="h", tokens_in=None, tokens_out=None, outcome=None,
        timestamp=None, cost_usd=0.5, model=None, parent_id=None,
        workflow=None, metadata={"synthesized_cost_only": True},
    )


def test_synthesized_cost_only_samples_are_spot_checkable():
    c = coverage([_synthesized_event_with_position("t1", 3)])
    assert len(c.synthesized_cost_only_samples) == 1
    assert "task=t1 step=3" in c.synthesized_cost_only_samples[0]


def test_synthesized_cost_only_samples_capped_at_max_samples():
    events = [_synthesized_event_with_position(f"t{i}", 0) for i in range(5)]
    c = compute_generic_coverage(events, max_samples=2)
    assert c.synthesized_cost_only_events == 5
    assert len(c.synthesized_cost_only_samples) == 2
