"""Tests for the top-level `redundo` dispatcher, and the adapt|analyze
pipe it exists to make visible -- see redundo/cli.py's own docstring.
"""

import json

from redundo.cli import main


def test_no_args_prints_usage_to_stderr_and_fails(capsys):
    exit_code = main([])
    assert exit_code == 1
    assert "Subcommands:" in capsys.readouterr().err


def test_help_prints_usage_to_stdout_and_succeeds(capsys):
    exit_code = main(["--help"])
    assert exit_code == 0
    assert "Subcommands:" in capsys.readouterr().out


def test_unknown_subcommand_fails_with_a_clear_message(capsys):
    exit_code = main(["frobnicate"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "unknown subcommand 'frobnicate'" in err
    assert "adapt, analyze, or collect" in err


def test_analyze_subcommand_dispatches_with_no_events_error(tmp_path, capsys):
    # An empty trace file is enough to prove `analyze` (not some other
    # code path) is what ran, without needing a full fixture here --
    # redundo.analyzer's own tests cover its behavior in depth.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    exit_code = main(["analyze", str(empty)])
    assert exit_code == 1
    assert "no events loaded" in capsys.readouterr().err


def test_adapt_subcommand_dispatches_on_a_missing_directory(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    exit_code = main(["adapt", str(missing)])
    assert exit_code == 1
    assert "not a directory" in capsys.readouterr().err


def test_adapt_piped_into_analyze_end_to_end(tmp_path, capsys, monkeypatch):
    # The actual pitch: `redundo adapt ... | redundo analyze` without
    # a file touching disk in between. Build one OpenInference-shaped OTLP
    # batch with a redundant repeated tool call, run it through `adapt`,
    # feed the resulting NDJSON straight into `analyze` via stdin -- a real
    # shell pipe isn't available inside a single test process, so stdin is
    # what stands in for it here (the same code path `analyze` actually
    # reads from when a shell pipes into it).
    doc = {
        "resourceSpans": [{
            "resource": {"attributes": []},
            "scopeSpans": [{"spans": [
                {
                    "traceId": "t1", "spanId": "s1", "name": "search",
                    "startTimeUnixNano": "1", "endTimeUnixNano": "2",
                    "attributes": [
                        {"key": "openinference.span.kind", "value": {"stringValue": "TOOL"}},
                        {"key": "input.value", "value": {"stringValue": "same query"}},
                    ],
                },
                {
                    "traceId": "t1", "spanId": "s2", "parentSpanId": "s1", "name": "search",
                    "startTimeUnixNano": "3", "endTimeUnixNano": "4",
                    "attributes": [
                        {"key": "openinference.span.kind", "value": {"stringValue": "TOOL"}},
                        {"key": "input.value", "value": {"stringValue": "same query"}},
                    ],
                },
            ]}],
        }],
    }
    otlp_dir = tmp_path / "otlp"
    otlp_dir.mkdir()
    (otlp_dir / "traces-1.json").write_text(json.dumps(doc), encoding="utf-8")

    adapt_exit = main(["adapt", str(otlp_dir)])
    assert adapt_exit == 0
    ndjson = capsys.readouterr().out
    assert ndjson.strip()

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(ndjson))
    analyze_exit = main(["analyze", "--format", "json"])
    assert analyze_exit == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_candidate_pairs"] == 1
