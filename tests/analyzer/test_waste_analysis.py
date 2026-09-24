from redundo.analyzer.analyses import WasteAnalysis
from redundo.analyzer.schema import Event


def make_event(step_index, event_type="tool_call", name="search", content_hash="h1",
                outcome=None, cost_usd=None, tokens_in=None, tokens_out=None,
                model=None, workflow=None, task_id="t1", metadata=None, timestamp=None):
    return Event(
        task_id=task_id,
        step_index=step_index,
        event_type=event_type,
        name=name,
        content_hash=content_hash,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        outcome=outcome,
        timestamp=timestamp,
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
        "cross_task_redundancy", "recurring_pattern",
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


# --- cross_task_redundancy / recurring_pattern ---------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _t(minutes):
    return _T0 + timedelta(minutes=minutes)


def test_linked_tasks_land_in_cross_task_redundancy():
    events = [
        make_event(0, task_id="parent", timestamp=_t(0)),
        make_event(
            0, task_id="child", timestamp=_t(10),
            metadata={"parent_task_id": "parent"},
        ),
    ]
    result = run(events)
    assert bucket(result, "cross_task_redundancy").slice.count == 1
    assert bucket(result, "recurring_pattern").slice.count == 0


def test_unrelated_tasks_land_in_recurring_pattern_not_cross_task_redundancy():
    events = [
        make_event(0, task_id="t1", timestamp=_t(0)),
        make_event(0, task_id="t2", timestamp=_t(10)),
    ]
    result = run(events)
    assert bucket(result, "recurring_pattern").slice.count == 1
    assert bucket(result, "cross_task_redundancy").slice.count == 0


def test_cross_task_pairs_never_double_count_same_task_repeats():
    # A same-task exact repeat must stay in confirmed_waste/etc., never
    # also show up in the two cross-task buckets.
    events = [
        make_event(0, event_type="tool_call", cost_usd=0.01, timestamp=_t(0)),
        make_event(1, event_type="tool_result", content_hash="same", timestamp=_t(1)),
        make_event(
            2, event_type="tool_call", cost_usd=0.02, outcome="error", timestamp=_t(2)
        ),
        make_event(
            3, event_type="tool_result", content_hash="same", outcome="error", timestamp=_t(3)
        ),
    ]
    result = run(events)
    assert bucket(result, "confirmed_waste").slice.count >= 1
    assert bucket(result, "cross_task_redundancy").slice.count == 0
    assert bucket(result, "recurring_pattern").slice.count == 0


def test_total_candidates_includes_cross_task_pairs():
    events = [
        make_event(0, task_id="t1", timestamp=_t(0)),
        make_event(0, task_id="t2", timestamp=_t(10)),
    ]
    result = run(events)
    assert result.total_candidates == 1


def test_cross_task_coverage_notes_are_separate_and_silent_when_absent():
    linked = [
        make_event(0, task_id="parent", timestamp=_t(0)),
        make_event(0, task_id="child", timestamp=_t(10), metadata={"parent_task_id": "parent"}),
    ]
    result = run(linked)
    notes = result.coverage.extra_notes
    assert any("same real workflow" in n for n in notes)
    assert not any("no confirmed relationship" in n for n in notes)

    unrelated = [
        make_event(0, task_id="t1", timestamp=_t(0)),
        make_event(0, task_id="t2", timestamp=_t(10)),
    ]
    result2 = run(unrelated)
    notes2 = result2.coverage.extra_notes
    assert any("no confirmed relationship" in n for n in notes2)
    assert not any("same real workflow" in n for n in notes2)


# --- insight_text -----------------------------------------------------------

def test_confirmed_waste_and_unclassified_get_insight_text_not_likely_legitimate():
    events = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = run(events)
    assert bucket(result, "confirmed_waste").insight_text is not None
    assert "stuck, not working" in bucket(result, "confirmed_waste").insight_text
    assert bucket(result, "likely_legitimate").insight_text is None
    assert bucket(result, "unclassified").insight_text is not None
    assert "call-level answer" in bucket(result, "unclassified").insight_text


# --- highlights ---------------------------------------------------------------

def _confirmed_waste_pair(task_id, cost_usd):
    return [
        make_event(0, event_type="tool_call", cost_usd=cost_usd, task_id=task_id),
        make_event(1, event_type="tool_result", content_hash="same", task_id=task_id),
        make_event(2, event_type="tool_call", cost_usd=cost_usd, outcome="error", task_id=task_id),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error",
                    task_id=task_id),
    ]


def test_highlights_rank_confirmed_waste_by_real_cost_descending():
    events = (
        _confirmed_waste_pair("cheap", 0.01)
        + _confirmed_waste_pair("expensive", 5.0)
        + _confirmed_waste_pair("medium", 1.0)
    )
    result = run(events)
    assert len(result.highlights) == 3
    assert result.highlights[0].startswith("1. $5.00")
    assert "task=expensive" in result.highlights[0]
    assert result.highlights[1].startswith("2. $1.00")
    assert "task=medium" in result.highlights[1]
    assert result.highlights[2].startswith("3. $0.0100")
    assert "task=cheap" in result.highlights[2]


def test_highlights_capped_at_three():
    events = []
    for i in range(5):
        events += _confirmed_waste_pair(f"t{i}", float(i + 1))
    result = run(events)
    assert len(result.highlights) == 3


def test_highlights_never_include_unpriced_confirmed_waste_pairs():
    events = [
        make_event(0, event_type="tool_call"),  # no cost_usd
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    result = run(events)
    assert bucket(result, "confirmed_waste").slice.count == 1
    assert result.highlights == []


def test_highlights_never_rank_other_buckets():
    # A likely_legitimate pair (result changed) with real cost -- must
    # never show up in "Fix these first", which is confirmed_waste only.
    events = [
        make_event(0, event_type="tool_call", cost_usd=9.0),
        make_event(1, event_type="tool_result", content_hash="a"),
        make_event(2, event_type="tool_call", cost_usd=9.0),
        make_event(3, event_type="tool_result", content_hash="b"),  # result changed
    ]
    result = run(events)
    assert bucket(result, "likely_legitimate").slice.count == 1
    assert result.highlights == []


# --- confidence_stat ----------------------------------------------------

def test_confidence_stat_none_when_no_exact_match_pairs_at_all():
    result = run([])
    assert result.confidence_stat is None


def test_confidence_stat_computed_over_the_three_verdict_buckets_only():
    # confirmed_waste (identical result, no write, task failed):
    confirmed = [
        make_event(0, event_type="tool_call"),
        make_event(1, event_type="tool_result", content_hash="same"),
        make_event(2, event_type="tool_call", outcome="error"),
        make_event(3, event_type="tool_result", content_hash="same", outcome="error"),
    ]
    # unclassified (no result correlation at all):
    unclassified = [
        make_event(0, event_type="tool_call", task_id="t2"),
        make_event(1, event_type="tool_call", task_id="t2"),
    ]
    result = run(confirmed + unclassified)
    assert bucket(result, "confirmed_waste").slice.count == 1
    assert bucket(result, "unclassified").slice.count == 1
    label, value, sub = result.confidence_stat
    assert label == "Verdicts reached"
    # n=2 is below the percent-display threshold: a bare fraction, not a
    # percentage that overclaims precision on two data points.
    assert value == "1/2"
    assert "exact repeats judged" in sub
    assert "1 unclassified" in sub


def test_confidence_stat_ignores_near_duplicate_and_cross_task_pairs():
    # Two near-identical (not identical) calls -- a near_duplicate pair,
    # never one of the three real Verdict buckets, so it must never
    # affect the confidence_stat denominator.
    zero_fp = "0" * 16
    close_fp = "f" * 2 + "0" * 14  # within default Hamming threshold
    events = [
        make_event(0, content_hash="a", metadata={"similarity_fingerprint": zero_fp}),
        make_event(1, content_hash="b", metadata={"similarity_fingerprint": close_fp}),
    ]
    result = run(events)
    assert bucket(result, "near_duplicate").slice.count == 1
    assert bucket(result, "confirmed_waste").slice.count == 0
    assert bucket(result, "likely_legitimate").slice.count == 0
    assert bucket(result, "unclassified").slice.count == 0
    assert result.confidence_stat is None
