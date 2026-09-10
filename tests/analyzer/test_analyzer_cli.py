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


def test_missing_trace_file_fails_with_a_clear_message_not_a_traceback(tmp_path, capsys):
    exit_code = main([str(tmp_path / "does-not-exist.jsonl")])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "file not found" in err
    # --lenient only helps with malformed rows, not a missing file --
    # showing it here would be misleading advice, not a helpful hint.
    assert "--lenient" not in err


def test_output_path_with_no_parent_directory_fails_with_a_clear_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TRACE))
    bad_output = tmp_path / "no-such-parent-dir" / "report.txt"
    exit_code = main(["-", "-o", str(bad_output)])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(bad_output) in err


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
