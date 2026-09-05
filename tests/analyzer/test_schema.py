import pytest

from redundo.analyzer.schema import Event, SchemaError


def base_row(**overrides):
    row = {
        "task_id": "t1",
        "step_index": 0,
        "event_type": "tool_call",
        "name": "search",
        "content_hash": "abc123",
    }
    row.update(overrides)
    return row


def test_minimal_row_parses_with_none_optionals():
    event = Event.from_dict(base_row())
    assert event.task_id == "t1"
    assert event.step_index == 0
    assert event.event_type == "tool_call"
    assert event.tokens_in is None
    assert event.outcome is None
    assert event.metadata == {}
    assert event.id == ("t1", 0)


def test_full_row_parses():
    row = base_row(
        tokens_in=100,
        tokens_out=20,
        outcome="ok",
        timestamp="2026-08-31T12:00:00Z",
        cost_usd=0.0012,
        model="gpt-5.6",
        parent_id=None,
        workflow="research_agent",
        metadata={"write": False},
    )
    event = Event.from_dict(row)
    assert event.tokens_in == 100
    assert event.outcome == "ok"
    assert event.cost_usd == 0.0012
    assert event.workflow == "research_agent"
    assert event.metadata == {"write": False}


@pytest.mark.parametrize("missing", ["task_id", "step_index", "event_type", "name", "content_hash"])
def test_missing_required_field_raises(missing):
    row = base_row()
    del row[missing]
    with pytest.raises(SchemaError):
        Event.from_dict(row)


def test_bad_event_type_raises():
    with pytest.raises(SchemaError):
        Event.from_dict(base_row(event_type="not_a_real_type"))


def test_bad_outcome_raises():
    with pytest.raises(SchemaError):
        Event.from_dict(base_row(outcome="maybe"))


def test_empty_outcome_is_none():
    event = Event.from_dict(base_row(outcome=""))
    assert event.outcome is None


def test_non_dict_metadata_raises():
    with pytest.raises(SchemaError):
        Event.from_dict(base_row(metadata="not a dict"))
