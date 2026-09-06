import io

import pytest

from redundo.analyzer.cli import main

TRACE = (
    '{"task_id": "t1", "step_index": 0, "event_type": "tool_call", "name": "x", '
    '"content_hash": "h1"}\n'
    '{"task_id": "t1", "step_index": 1, "event_type": "tool_call", "name": "x", '
    '"content_hash": "h1", "outcome": "ok"}\n'
)


def test_trace_arg_omitted_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TRACE))
    exit_code = main([])
    assert exit_code == 0
    assert "Candidate redundant-repeat pairs: 1" in capsys.readouterr().out


def test_trace_arg_dash_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TRACE))
    exit_code = main(["-"])
    assert exit_code == 0
    assert "Candidate redundant-repeat pairs: 1" in capsys.readouterr().out


def test_trace_arg_path_still_reads_a_file(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    path.write_text(TRACE, encoding="utf-8")
    exit_code = main([str(path)])
    assert exit_code == 0
    assert "Candidate redundant-repeat pairs: 1" in capsys.readouterr().out


def test_unknown_format_fails_with_dynamic_name_list(monkeypatch, capsys):
    # --format's choices come from ReportFormatRegistry.names() now, not a
    # hardcoded tuple -- argparse still rejects an unknown one before
    # main()'s body ever runs.
    monkeypatch.setattr("sys.stdin", io.StringIO(TRACE))
    with pytest.raises(SystemExit) as exc_info:
        main(["--format", "bogus"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "html" in err and "json" in err and "text" in err
