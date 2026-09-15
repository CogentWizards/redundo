import json

from redundo.analyzer.drift_cli import main


def _write(tmp_path, events):
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _event(task_id, step_index, name="a", parent_task_id=None, link_kind=None):
    metadata = {}
    if parent_task_id is not None:
        metadata["parent_task_id"] = parent_task_id
    if link_kind is not None:
        metadata["parent_task_link_kind"] = link_kind
    return {
        "task_id": task_id, "step_index": step_index, "event_type": "tool_call",
        "name": name, "content_hash": "h", "metadata": metadata,
    }


def test_no_continuation_chains_reports_none_found(tmp_path, capsys):
    path = _write(tmp_path, [_event("t1", 0), _event("t2", 0)])
    exit_code = main([str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No continuation chains found" in out
    assert "heuristic, not a finding" in out


def test_continuation_chain_reported_in_text_format(tmp_path, capsys):
    events = [
        _event("s1", 0, name="search"),
        _event("s2", 0, name="search", parent_task_id="s1", link_kind="continuation"),
    ]
    path = _write(tmp_path, events)
    exit_code = main([str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Chain: s1 -> s2 (2 tasks)" in out
    assert "shape distance: 0 step(s) changed" in out


def test_json_format_is_well_formed(tmp_path, capsys):
    events = [
        _event("s1", 0, name="search"),
        _event("s2", 0, name="retry_search", parent_task_id="s1", link_kind="continuation"),
    ]
    path = _write(tmp_path, events)
    exit_code = main([str(path), "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["chains"][0]["tasks"] == ["s1", "s2"]
    hop = data["chains"][0]["hops"][0]
    assert hop["predecessor_task_id"] == "s1"
    assert hop["successor_task_id"] == "s2"
    assert hop["substituted"][0]["predecessor"]["name"] == "search"
    assert hop["substituted"][0]["successor"]["name"] == "retry_search"


def test_no_events_loaded_is_an_error(tmp_path, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    exit_code = main([str(path)])
    assert exit_code == 1
    assert "no events loaded" in capsys.readouterr().err


def test_delegation_only_link_reports_no_chains(tmp_path, capsys):
    events = [
        _event("parent", 0),
        _event("child", 0, parent_task_id="parent", link_kind="delegation"),
    ]
    path = _write(tmp_path, events)
    exit_code = main([str(path)])
    assert exit_code == 0
    assert "No continuation chains found" in capsys.readouterr().out
