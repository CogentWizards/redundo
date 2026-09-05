import json

from redundo.adapter.cli import main
from helpers import span, traces_document


def _write(tmp_path, name, doc):
    (tmp_path / name).write_text(json.dumps(doc), encoding="utf-8")


def test_cli_auto_detects_and_writes_jsonl_to_stdout(tmp_path, capsys):
    doc = traces_document([
        span("s1", name="claude_code.interaction", start=0,
             attributes={"session.id": "sess-1", "user_prompt": "hi"}),
        span("s2", parent_span_id="s1", name="claude_code.llm_request", start=1,
             attributes={"session.id": "sess-1", "model": "m", "success": True}),
    ])
    _write(tmp_path, "traces-1.json", doc)

    exit_code = main([str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.strip().splitlines()]
    assert len(records) == 1
    assert records[0]["event_type"] == "llm_call"


def test_cli_writes_to_output_file(tmp_path):
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    _write(tmp_path, "traces-1.json", doc)
    out_path = tmp_path / "out.jsonl"

    exit_code = main([str(tmp_path), "--output", str(out_path)])
    assert exit_code == 0
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_cli_summary_flag_reports_detected_source(tmp_path, capsys):
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    _write(tmp_path, "traces-1.json", doc)

    exit_code = main([str(tmp_path), "--summary"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "source: openinference (detected via" in err


def test_cli_source_flag_skips_detection(tmp_path, capsys):
    # A span shape detect_source() would call OpenInference -- but forced
    # to claude-code, the OpenInference-specific content never converts to
    # anything (no claude_code.* structure present), so it should produce
    # zero records without erroring.
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    _write(tmp_path, "traces-1.json", doc)

    exit_code = main([str(tmp_path), "--source", "claude-code", "--summary"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "source: claude-code (detected via --source flag)" in err


def test_cli_combines_multiple_batch_files_in_one_directory(tmp_path, capsys):
    doc1 = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "a"}),
    ])
    doc2 = traces_document([
        span("s2", start=1, attributes={"openinference.span.kind": "LLM", "input.value": "b"}),
    ])
    _write(tmp_path, "traces-1.json", doc1)
    _write(tmp_path, "traces-2.json", doc2)

    exit_code = main([str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.strip().splitlines()]
    assert len(records) == 2


def test_cli_ignores_unrecognized_json_files(tmp_path, capsys):
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    _write(tmp_path, "traces-1.json", doc)
    _write(tmp_path, "unrelated.json", {"something": "else"})

    exit_code = main([str(tmp_path), "--summary"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "1 file(s)" in err
    assert "were not recognized" in err


def test_cli_fails_on_missing_directory(tmp_path, capsys):
    exit_code = main([str(tmp_path / "does-not-exist")])
    assert exit_code == 1
    assert "not a directory" in capsys.readouterr().err


def test_cli_fails_on_empty_directory(tmp_path, capsys):
    exit_code = main([str(tmp_path)])
    assert exit_code == 1
    assert "no OTLP trace or log documents found" in capsys.readouterr().err


def test_cli_fails_loudly_when_source_undetectable(tmp_path, capsys):
    doc = traces_document([span("s1", name="something.unrelated", start=0)])
    _write(tmp_path, "traces-1.json", doc)

    exit_code = main([str(tmp_path)])
    assert exit_code == 1
    assert "--source" in capsys.readouterr().err


def test_cli_fails_on_invalid_json(tmp_path, capsys):
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    exit_code = main([str(tmp_path)])
    assert exit_code == 1
