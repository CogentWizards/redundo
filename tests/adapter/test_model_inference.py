from redundo.adapter.model_inference import fill_missing_tool_model


def _record(event_type, task_id="t1", workflow="main", model=None):
    return {
        "task_id": task_id,
        "event_type": event_type,
        "workflow": workflow,
        "model": model,
        "metadata": {},
    }


def test_tool_call_after_llm_call_gets_preceding_model():
    records = [
        _record("llm_call", model="opus"),
        _record("tool_call"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] == "opus"
    assert records[1]["metadata"]["model_basis"] == "preceding_llm_call"


def test_tool_call_before_llm_call_gets_following_model():
    records = [
        _record("tool_call"),
        _record("llm_call", model="opus"),
    ]
    fill_missing_tool_model(records)
    assert records[0]["model"] == "opus"
    assert records[0]["metadata"]["model_basis"] == "following_llm_call"


def test_tool_call_prefers_nearer_preceding_over_farther_following():
    records = [
        _record("llm_call", model="haiku"),
        _record("tool_call"),
        _record("llm_call", model="opus"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] == "haiku"
    assert records[1]["metadata"]["model_basis"] == "preceding_llm_call"


def test_tool_call_prefers_nearer_following_over_farther_preceding():
    records = [
        _record("llm_call", model="haiku"),
        _record("tool_call"),
        _record("tool_call"),
        _record("llm_call", model="opus"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] == "haiku"
    assert records[2]["model"] == "opus"


def test_equidistant_prefers_preceding_on_tie():
    records = [
        _record("llm_call", model="haiku"),
        _record("tool_call"),
        _record("llm_call", model="opus"),
    ]
    # tool_call at index 1 is distance 1 from both neighbors -- preceding wins.
    fill_missing_tool_model(records)
    assert records[1]["model"] == "haiku"
    assert records[1]["metadata"]["model_basis"] == "preceding_llm_call"


def test_no_llm_call_anywhere_in_group_leaves_model_none():
    records = [_record("tool_call"), _record("tool_result")]
    fill_missing_tool_model(records)
    assert records[0]["model"] is None
    assert "model_basis" not in records[0]["metadata"]
    assert records[1]["model"] is None
    assert "model_basis" not in records[1]["metadata"]


def test_scoped_per_task_not_shared_across_tasks():
    records = [
        _record("llm_call", task_id="t1", model="opus"),
        _record("tool_call", task_id="t2"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] is None


def test_scoped_per_workflow_not_shared_across_sibling_branches():
    records = [
        _record("llm_call", workflow="branch-a", model="haiku"),
        _record("tool_call", workflow="branch-b"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] is None


def test_existing_model_is_never_overwritten():
    records = [
        _record("llm_call", model="opus"),
        _record("tool_call", model="already-set"),
    ]
    fill_missing_tool_model(records)
    assert records[1]["model"] == "already-set"
    assert "model_basis" not in records[1]["metadata"]


def test_llm_call_records_are_never_touched():
    records = [_record("llm_call", model=None), _record("llm_call", model="opus")]
    fill_missing_tool_model(records)
    assert records[0]["model"] is None
    assert "model_basis" not in records[0]["metadata"]
