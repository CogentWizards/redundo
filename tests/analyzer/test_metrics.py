from redundo.analyzer.classify import Verdict, classify_pair
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.lineage import group_by_task
from redundo.analyzer.metrics import build_report
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                outcome=None, cost_usd=None, tokens_in=None, tokens_out=None,
                model=None, workflow=None, task_id="t1"):
    return Event(
        task_id=task_id,
        step_index=step_index,
        event_type=event_type,
        name=name,
        content_hash=content_hash,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        outcome=outcome,
        timestamp=None,
        cost_usd=cost_usd,
        model=model,
        parent_id=None,
        workflow=workflow,
        metadata={},
    )


def run(events):
    lineages = group_by_task(events)
    pairs = find_candidate_pairs(events)
    classifications = [classify_pair(p, lineages[p.task_id]) for p in pairs]
    return build_report(classifications, events)


def test_repeat_side_of_pair_is_what_gets_counted():
    # Three identical calls: two pairs, (A,B) and (B,C). Waste is
    # attributed to B and C (the repeats), not A -- two units, not three.
    events = [
        make_event(0, event_type="tool_call", cost_usd=0.01),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", cost_usd=0.02),
        make_event(3, event_type="tool_result", content_hash="same"),
        make_event(4, event_type="tool_call", cost_usd=0.03, outcome="error"),
        make_event(5, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    report = run(events)
    waste = report.by_verdict[Verdict.CONFIRMED_WASTE]
    assert waste.count == 2
    # repeats are the calls at step 2 (cost 0.02) and step 4 (cost 0.03)
    assert round(waste.cost_usd, 6) == 0.05


def test_unpriced_repeat_falls_back_to_tokens_and_unpriced_count():
    events = [
        make_event(0, event_type="tool_call", tokens_in=100, tokens_out=10, outcome=None),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", tokens_in=100, tokens_out=10, outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    report = run(events)
    waste = report.by_verdict[Verdict.CONFIRMED_WASTE]
    assert waste.count == 1
    assert waste.cost_usd == 0.0
    assert waste.unpriced_count == 1
    assert waste.tokens_in == 100
    assert waste.tokens_out == 10


def test_segmentation_by_model_and_workflow():
    events = [
        make_event(0, event_type="tool_call", model="gpt-5.6", workflow="researcher"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", model="gpt-5.6", workflow="researcher", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    report = run(events)
    waste_by_model = report.by_verdict_and_model[Verdict.CONFIRMED_WASTE]
    waste_by_workflow = report.by_verdict_and_workflow[Verdict.CONFIRMED_WASTE]
    assert waste_by_model["gpt-5.6"].count == 1
    assert waste_by_workflow["researcher"].count == 1


def test_missing_model_and_workflow_get_placeholder_keys():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    report = run(events)
    waste_by_model = report.by_verdict_and_model[Verdict.CONFIRMED_WASTE]
    assert "(unknown model)" in waste_by_model


def test_report_as_dict_is_json_serializable_shape():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    report = run(events)
    d = report.as_dict()
    assert d["total_candidate_pairs"] == 1
    assert set(d["by_verdict"].keys()) == {"confirmed_waste", "likely_legitimate", "unclassified"}
    assert d["by_verdict"]["confirmed_waste"]["count"] == 1
