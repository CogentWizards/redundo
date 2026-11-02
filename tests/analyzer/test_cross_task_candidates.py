import random
from datetime import datetime, timedelta, timezone

from redundo.analyzer import cross_task_candidates
from redundo.analyzer.cross_task_candidates import find_cross_task_pairs
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.near_duplicates import DEFAULT_SIMILARITY_THRESHOLD, find_near_duplicate_pairs
from redundo.analyzer.schema import Event

ZERO_FP = "0" * 16
CLOSE_FP = "f" * 2 + "0" * 14  # distance 8 from ZERO_FP
FAR_FP = "f" * 8 + "0" * 8  # distance 32 from ZERO_FP

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _t(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def make_event(
    task_id, step_index, minutes, event_type="tool_call", name="search",
    content_hash="h", fingerprint=None, parent_id=None,
):
    metadata = {}
    if fingerprint is not None:
        metadata["similarity_fingerprint"] = fingerprint
    return Event(
        task_id=task_id, step_index=step_index, event_type=event_type, name=name,
        content_hash=content_hash, tokens_in=None, tokens_out=None, outcome=None,
        timestamp=_t(minutes), cost_usd=None, model=None, parent_id=parent_id,
        workflow=None, metadata=metadata,
    )


def test_exact_match_across_two_tasks_is_a_cross_task_pair():
    events = [
        make_event("t1", 0, minutes=0),
        make_event("t2", 0, minutes=10),
    ]
    pairs = find_cross_task_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.task_id == "t1"
    assert pairs[0].repeat.task_id == "t2"
    assert pairs[0].hamming_distance == 0


def test_same_task_repeat_is_never_a_cross_task_pair():
    events = [
        make_event("t1", 0, minutes=0),
        make_event("t1", 1, minutes=10),
    ]
    assert find_cross_task_pairs(events) == []


def test_already_claimed_by_cycles_is_excluded():
    events = [
        make_event("t1", 0, minutes=0, parent_id=None),
        make_event("t1", 1, minutes=1, parent_id=0),
        make_event("t2", 0, minutes=10),
    ]
    exact_pairs = find_candidate_pairs(events)
    assert len(exact_pairs) == 1  # t1's own step 0 -> step 1, same-task lineage repeat
    cross_pairs = find_cross_task_pairs(events, exact_pairs=exact_pairs)
    # t1 step 1 is already claimed by cycles.py; only t2 can still match t1's
    # original (step 0), which it does.
    assert len(cross_pairs) == 1
    assert cross_pairs[0].original.task_id == "t1"
    assert cross_pairs[0].original.step_index == 0
    assert cross_pairs[0].repeat.task_id == "t2"


def test_nearest_in_time_wins_across_three_tasks():
    events = [
        make_event("t1", 0, minutes=0),
        make_event("t2", 0, minutes=10),
        make_event("t3", 0, minutes=20),
    ]
    pairs = find_cross_task_pairs(events)
    by_repeat = {p.repeat.task_id: p for p in pairs}
    assert by_repeat["t2"].original.task_id == "t1"
    assert by_repeat["t3"].original.task_id == "t2"  # nearest, not t1


def test_no_timestamp_is_excluded_entirely():
    a = make_event("t1", 0, minutes=0)
    b = make_event("t2", 0, minutes=10)
    b = Event(
        task_id=b.task_id, step_index=b.step_index, event_type=b.event_type, name=b.name,
        content_hash=b.content_hash, tokens_in=None, tokens_out=None, outcome=None,
        timestamp=None, cost_usd=None, model=None, parent_id=None, workflow=None,
        metadata=b.metadata,
    )
    assert find_cross_task_pairs([a, b]) == []


def test_near_duplicate_across_tasks_is_found():
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=CLOSE_FP),
    ]
    pairs = find_cross_task_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.task_id == "t1"
    assert pairs[0].repeat.task_id == "t2"
    assert pairs[0].hamming_distance == 8


def test_near_duplicate_beyond_threshold_is_not_paired():
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=FAR_FP),
    ]
    assert find_cross_task_pairs([events[0], events[1]], threshold=24) == []


def test_near_duplicate_excludes_events_already_claimed_by_same_task_near_duplicates():
    # A same-task near-duplicate pair must not also surface here.
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t1", 1, minutes=1, content_hash="h2", fingerprint=CLOSE_FP, parent_id=0),
    ]
    near_pairs = find_near_duplicate_pairs(events)
    assert len(near_pairs) == 1
    assert find_cross_task_pairs(events, near_pairs=near_pairs) == []


def test_missing_fingerprint_never_produces_a_near_duplicate_cross_task_match():
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=None),
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=None),
    ]
    assert find_cross_task_pairs(events) == []


def test_llm_call_and_tool_result_rows_are_never_candidates():
    events = [
        make_event("t1", 0, minutes=0, event_type="tool_result", content_hash="h1"),
        make_event("t2", 0, minutes=10, event_type="tool_result", content_hash="h1"),
    ]
    assert find_cross_task_pairs(events) == []


# Two differing bits in each of the four 16-bit bands: distance 8, well
# inside the threshold, yet no band is shared. Banding alone never found it.
EVERY_BAND_FP = "0003000300030003"


def test_near_duplicate_differing_in_every_band_is_still_found():
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=EVERY_BAND_FP),
    ]
    pairs = find_cross_task_pairs(events)
    assert len(pairs) == 1
    assert pairs[0].original.task_id == "t1"
    assert pairs[0].hamming_distance == 8


def test_near_duplicate_at_exactly_the_threshold_is_paired():
    at_threshold = "00ff00ff00ff0000"  # 24 bits, spread over three bands
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=at_threshold),
    ]
    pairs = find_cross_task_pairs(events, threshold=24)
    assert [p.hamming_distance for p in pairs] == [24]
    assert find_cross_task_pairs(events, threshold=23) == []


def test_near_duplicate_picks_nearest_in_time_qualifying_predecessor():
    events = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=5, content_hash="h2", fingerprint=EVERY_BAND_FP),
        make_event("t3", 0, minutes=7, content_hash="h3", fingerprint=FAR_FP),
        make_event("t4", 0, minutes=10, content_hash="h4", fingerprint="0001000100010001"),
    ]
    by_repeat = {p.repeat.task_id: p for p in find_cross_task_pairs(events)}
    # t3 is nearer in time but 36 bits away from t4, so t2 wins.
    assert by_repeat["t4"].original.task_id == "t2"
    assert by_repeat["t4"].hamming_distance == 4


def test_near_duplicate_never_pairs_different_names():
    events = [
        make_event("t1", 0, minutes=0, name="search", content_hash="h1", fingerprint=ZERO_FP),
        make_event("t2", 0, minutes=10, name="read", content_hash="h2", fingerprint=EVERY_BAND_FP),
    ]
    assert find_cross_task_pairs(events) == []


def test_predecessor_older_than_the_window_is_reached_through_bands(monkeypatch):
    monkeypatch.setattr(cross_task_candidates, "_EXHAUSTIVE_WINDOW", 2)
    filler = [
        make_event("t9", i, minutes=1 + i, content_hash=f"f{i}", fingerprint=FAR_FP)
        for i in range(3)
    ]
    shares_a_band = "0000000000000007"  # 3 bits, all in band 0
    every_band = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        *filler,
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=EVERY_BAND_FP),
    ]
    one_band = [
        make_event("t1", 0, minutes=0, content_hash="h1", fingerprint=ZERO_FP),
        *filler,
        make_event("t2", 0, minutes=10, content_hash="h2", fingerprint=shares_a_band),
    ]
    # Past the window only the band backstop can reach t1: it finds the
    # pair that shares a band and, by design, not the one that doesn't.
    assert [p.repeat.task_id for p in find_cross_task_pairs(one_band)] == ["t2"]
    assert all(p.repeat.task_id != "t2" for p in find_cross_task_pairs(every_band))


def test_near_duplicate_search_matches_brute_force_on_random_fingerprints():
    rng = random.Random(7)
    base = rng.getrandbits(64)
    events = []
    for i in range(120):
        fp = base
        for bit in rng.sample(range(64), rng.randint(0, 30)):
            fp ^= 1 << bit
        events.append(make_event(
            f"t{i % 7}", i, minutes=i, content_hash=f"h{i}", fingerprint=f"{fp:016x}",
        ))

    expected = {}
    for repeat in events:
        best = None
        for original in events:
            if original.task_id == repeat.task_id or original.timestamp >= repeat.timestamp:
                continue
            fp_a = int(repeat.metadata["similarity_fingerprint"], 16)
            fp_b = int(original.metadata["similarity_fingerprint"], 16)
            if bin(fp_a ^ fp_b).count("1") <= DEFAULT_SIMILARITY_THRESHOLD and (best is None or original.timestamp > best.timestamp):
                best = original
        if best is not None:
            expected[(repeat.task_id, repeat.step_index)] = (best.task_id, best.step_index)

    found = {
        (p.repeat.task_id, p.repeat.step_index): (p.original.task_id, p.original.step_index)
        for p in find_cross_task_pairs(events)
    }
    assert found == expected
