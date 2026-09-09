from redundo.analyzer.analyses import WasteAnalysis
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                outcome=None, cost_usd=None, tokens_in=None, tokens_out=None,
                model=None, workflow=None, task_id="t1", metadata=None):
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
        metadata=metadata if metadata is not None else {},
    )


def run(events):
    return WasteAnalysis().run(events)


def bucket(result, key):
    return next(b for b in result.buckets if b.key == key)


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
    result = run(events)
    waste = bucket(result, "confirmed_waste").slice
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
    result = run(events)
    waste = bucket(result, "confirmed_waste").slice
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
    result = run(events)
    waste_by_model = result.by_bucket_and_model["confirmed_waste"]
    waste_by_workflow = result.by_bucket_and_workflow["confirmed_waste"]
    assert waste_by_model["gpt-5.6"].count == 1
    assert waste_by_workflow["researcher"].count == 1


def test_missing_model_and_workflow_get_placeholder_keys():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = run(events)
    waste_by_model = result.by_bucket_and_model["confirmed_waste"]
    assert "(unknown model)" in waste_by_model


def test_result_as_dict_is_json_serializable_shape():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = run(events)
    d = result.as_dict()
    assert d["total_candidates"] == 1
    assert set(d["by_bucket"].keys()) == {
        "confirmed_waste", "likely_legitimate", "unclassified", "near_duplicate",
    }
    assert d["by_bucket"]["confirmed_waste"]["count"] == 1


# --- comparability: tasks with vs. without a candidate pair ---------------
# Moved from the old test_coverage.py -- this is waste-analysis-specific
# (candidate pairs are this analysis's own concept), computed by
# WasteAnalysis itself into coverage.extra_notes rather than a generic
# CoverageStats field.

def test_task_with_no_repeats_notes_full_comparability_gap():
    events = [
        make_event(0, task_id="t1", content_hash="a", cost_usd=1.0),
        make_event(1, task_id="t1", content_hash="b", cost_usd=2.0),
    ]
    result = run(events)
    note = result.coverage.extra_notes[0]
    assert "0/1 tasks" in note
    assert "$3.00" in note


def test_task_with_a_repeat_has_no_comparability_note():
    events = [
        make_event(0, task_id="t1", content_hash="a", cost_usd=1.0),
        make_event(1, task_id="t1", content_hash="a", cost_usd=1.0, outcome="ok"),
    ]
    result = run(events)
    assert result.coverage.extra_notes == []


def test_mixed_tasks_split_correctly_in_comparability_note():
    events = [
        make_event(0, task_id="t1", content_hash="a", cost_usd=1.0),
        make_event(1, task_id="t1", content_hash="a", cost_usd=1.0, outcome="ok"),
        make_event(0, task_id="t2", content_hash="x", cost_usd=5.0),
        make_event(1, task_id="t2", content_hash="y", cost_usd=5.0),
    ]
    result = run(events)
    assert any("1/2 tasks" in n for n in result.coverage.extra_notes)
    # Sanity: the task WITH a pair's cost is not double-counted into the
    # generic coverage total -- that's compute_generic_coverage()'s job,
    # untouched by this analysis-specific note.
    assert result.coverage.total_priced_cost_usd == 12.0


def test_comparability_note_ignores_unpriced_events():
    events = [
        make_event(0, task_id="t1", content_hash="a", cost_usd=None),
        make_event(1, task_id="t1", content_hash="b", cost_usd=None),
    ]
    result = run(events)
    note = result.coverage.extra_notes[0]
    assert "0/1 tasks" in note
    assert "$0.0000" in note  # nothing priced to sum
    assert "2 event(s)" in note


# --- 4th bucket: near_duplicate ---------------------------------------------

_ZERO_FP = "0" * 16
_CLOSE_FP = "f" * 2 + "0" * 14  # distance 8 from _ZERO_FP -- within default threshold


def test_exact_and_near_duplicate_pairs_land_in_disjoint_buckets():
    events = [
        # An exact-match pair, paired with tool_results and a failed
        # outcome -- the full confirmed_waste shape, same as
        # test_repeat_side_of_pair_is_what_gets_counted above.
        make_event(0, task_id="t1", event_type="tool_call", cost_usd=0.01),
        make_event(1, task_id="t1", event_type="tool_result", content_hash="same"),
        make_event(
            2, task_id="t1", event_type="tool_call", cost_usd=0.02, outcome="error"
        ),
        make_event(
            3, task_id="t1", event_type="tool_result", content_hash="same", outcome="error"
        ),
        # A near-duplicate pair in a different task -- different content_hash,
        # close fingerprints.
        make_event(
            0, task_id="t2", content_hash="a", cost_usd=0.03,
            metadata={"similarity_fingerprint": _ZERO_FP},
        ),
        make_event(
            1, task_id="t2", content_hash="b", cost_usd=0.04,
            metadata={"similarity_fingerprint": _CLOSE_FP},
        ),
    ]
    result = run(events)
    exact_bucket = bucket(result, "confirmed_waste")
    near_bucket = bucket(result, "near_duplicate")
    assert exact_bucket.slice.count == 1
    assert near_bucket.slice.count == 1
    assert result.total_candidates == 2


def test_near_duplicate_bucket_empty_when_nothing_similar():
    events = [
        make_event(0, content_hash="a", metadata={"similarity_fingerprint": _ZERO_FP}),
        make_event(1, content_hash="b", metadata={"similarity_fingerprint": "f" * 16}),
    ]
    result = run(events)
    assert bucket(result, "near_duplicate").slice.count == 0
    assert not any("near-duplicate" in n for n in result.coverage.extra_notes)


def test_near_duplicate_comparability_note_is_separate_from_exact_match_note():
    events = [
        make_event(
            0, task_id="t1", content_hash="a",
            metadata={"similarity_fingerprint": _ZERO_FP},
        ),
        make_event(
            1, task_id="t1", content_hash="b",
            metadata={"similarity_fingerprint": _CLOSE_FP},
        ),
    ]
    result = run(events)
    notes = result.coverage.extra_notes
    exact_note = next(n for n in notes if "repeated call for redundancy" in n)
    near_note = next(n for n in notes if "near-duplicate" in n)
    assert exact_note is not near_note
    assert "0/1 tasks" in exact_note  # no exact pair here
    assert "1/1 tasks" in near_note


def test_near_duplicate_threshold_is_configurable():
    events = [
        make_event(0, content_hash="a", metadata={"similarity_fingerprint": _ZERO_FP}),
        make_event(1, content_hash="b", metadata={"similarity_fingerprint": _CLOSE_FP}),
    ]
    strict_result = WasteAnalysis(near_duplicate_threshold=2).run(events)
    lenient_result = WasteAnalysis(near_duplicate_threshold=20).run(events)
    assert bucket(strict_result, "near_duplicate").slice.count == 0
    assert bucket(lenient_result, "near_duplicate").slice.count == 1
