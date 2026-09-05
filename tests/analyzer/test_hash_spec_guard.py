import pytest

from redundo.analyzer.ingest import IngestError, check_consistent_hash_spec
from redundo.analyzer.schema import Event


def make_event(step_index, hash_spec=None, task_id="t1"):
    return Event(
        task_id=task_id, step_index=step_index, event_type="tool_call", name="search",
        content_hash="h1", tokens_in=None, tokens_out=None, outcome=None, timestamp=None,
        cost_usd=None, model=None, parent_id=None, workflow=None,
        metadata={"hash_spec": hash_spec} if hash_spec else {},
    )


def test_no_hash_spec_anywhere_is_fine():
    events = [make_event(0), make_event(1)]
    assert check_consistent_hash_spec(events) is None


def test_single_consistent_hash_spec_is_fine():
    events = [make_event(0, hash_spec="v1"), make_event(1, hash_spec="v1")]
    assert check_consistent_hash_spec(events) == "v1"


def test_mixed_hash_spec_values_raise():
    events = [make_event(0, hash_spec="v1"), make_event(1, hash_spec="v2")]
    with pytest.raises(IngestError, match="inconsistent content_hash procedures"):
        check_consistent_hash_spec(events)


def test_some_present_some_absent_is_fine_if_present_values_agree():
    events = [make_event(0, hash_spec="v1"), make_event(1)]
    assert check_consistent_hash_spec(events) == "v1"
