import io
from pathlib import Path

import pytest

from redundo.analyzer.ingest import IngestError, load_events

FIXTURES = Path(__file__).parent / "fixtures"


def test_strict_raises_on_first_bad_row():
    with pytest.raises(IngestError):
        load_events(FIXTURES / "malformed.jsonl", strict=True)


def test_lenient_skips_bad_rows_and_records_errors():
    errors: list[str] = []
    events = load_events(FIXTURES / "malformed.jsonl", strict=False, on_error=errors)
    assert [e.step_index for e in events] == [0, 2]
    assert len(errors) == 2  # bad event_type + invalid JSON


def test_events_sorted_by_task_then_step():
    events = load_events(FIXTURES / "sample.jsonl")
    for a, b in zip(events, events[1:]):
        assert (a.task_id, a.step_index) <= (b.task_id, b.step_index)


def test_load_events_refuses_mixed_hash_spec(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        '{"task_id": "t1", "step_index": 0, "event_type": "tool_call", "name": "x", '
        '"content_hash": "h1", "metadata": {"hash_spec": "v1"}}\n'
        '{"task_id": "t1", "step_index": 1, "event_type": "tool_call", "name": "x", '
        '"content_hash": "h1", "metadata": {"hash_spec": "v2"}}\n',
        encoding="utf-8",
    )
    with pytest.raises(IngestError, match="inconsistent content_hash procedures"):
        load_events(path)


def test_load_events_accepts_an_open_stream_not_just_a_path():
    # This is what makes `redundo adapt | redundo analyze` work --
    # the CLI hands this an already-open sys.stdin, not a path.
    stream = io.StringIO(
        '{"task_id": "t1", "step_index": 0, "event_type": "tool_call", "name": "x", '
        '"content_hash": "h1"}\n'
    )
    events = load_events(stream)
    assert len(events) == 1
    assert events[0].task_id == "t1"
