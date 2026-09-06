"""Proves examples/redundo-plugin-example actually works, end to end,
through the real CLI -- not just that its classes/functions are
importable. This is what keeps docs/plugins.md's walkthrough honest: if
this package's entry points ever stop resolving, or its code drifts from
what the docs describe, this test is what notices.

redundo-plugin-example is installed editable as a dev dependency
(pyproject.toml's [tool.uv.sources]) specifically so its entry points are
genuinely discovered via importlib.metadata, the same as any real
third-party install -- nothing here is monkeypatched.
"""

import json

import pytest

from redundo.cli import main


def test_example_source_appears_in_adapt_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["adapt", "--help"])
    assert exc_info.value.code == 0
    assert "example" in capsys.readouterr().out


def _example_otlp_dir(tmp_path):
    doc = {
        "resourceSpans": [{
            "resource": {"attributes": []},
            "scopeSpans": [{"spans": [
                {
                    "traceId": "t1", "spanId": "s1", "name": "example.event",
                    "startTimeUnixNano": "0", "endTimeUnixNano": "1", "attributes": [],
                },
                {
                    "traceId": "t1", "spanId": "s2", "name": "example.event",
                    "startTimeUnixNano": "2", "endTimeUnixNano": "3", "attributes": [],
                },
            ]}],
        }],
    }
    otlp_dir = tmp_path / "otlp"
    otlp_dir.mkdir()
    (otlp_dir / "traces-1.json").write_text(json.dumps(doc), encoding="utf-8")
    return otlp_dir


def test_example_source_auto_detected_and_converted(tmp_path, capsys):
    otlp_dir = _example_otlp_dir(tmp_path)
    exit_code = main(["adapt", str(otlp_dir), "--summary"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "source: example" in captured.err
    records = [json.loads(line) for line in captured.out.strip().splitlines()]
    assert len(records) == 2
    assert all(r["name"] == "example_tool" for r in records)


def test_count_analysis_and_csv_format_end_to_end(tmp_path, capsys):
    otlp_dir = _example_otlp_dir(tmp_path)
    trace_path = tmp_path / "trace.jsonl"

    adapt_exit = main(["adapt", str(otlp_dir), "-o", str(trace_path)])
    assert adapt_exit == 0

    analyze_exit = main([
        "analyze", str(trace_path), "--analysis", "count", "--format", "csv",
    ])
    assert analyze_exit == 0
    output = capsys.readouterr().out
    assert "bucket,count,cost_usd,tokens_in,tokens_out" in output
    assert "all_events,2," in output
