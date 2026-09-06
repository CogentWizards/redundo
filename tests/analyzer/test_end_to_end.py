from pathlib import Path

import pytest

from redundo.analyzer.analyses import WasteAnalysis
from redundo.analyzer.classify import Verdict, classify_pair
from redundo.analyzer.cli import main
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.ingest import load_events
from redundo.analyzer.lineage import group_by_task

FIXTURES = Path(__file__).parent / "fixtures"


def test_sample_fixture_lands_in_expected_buckets():
    events = load_events(FIXTURES / "sample.jsonl")
    lineages = group_by_task(events)
    pairs = find_candidate_pairs(events)
    classifications = [classify_pair(p, lineages[p.task_id]) for p in pairs]

    by_task = {c.pair.task_id: c.verdict for c in classifications}
    assert by_task["confirmed-1"] == Verdict.CONFIRMED_WASTE
    assert by_task["legit-write-1"] == Verdict.LIKELY_LEGITIMATE
    assert by_task["legit-result-1"] == Verdict.LIKELY_LEGITIMATE
    assert by_task["legit-success-1"] == Verdict.LIKELY_LEGITIMATE
    assert by_task["unclassified-1"] == Verdict.UNCLASSIFIED

    result = WasteAnalysis().run(events)
    assert result.total_candidates == 5
    buckets = {b.key: b.slice for b in result.buckets}
    assert buckets["confirmed_waste"].count == 1
    assert buckets["likely_legitimate"].count == 3
    assert buckets["unclassified"].count == 1


def test_cli_text_output(capsys):
    exit_code = main([str(FIXTURES / "sample.jsonl")])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "confirmed_waste" in captured
    assert "likely_legitimate" in captured
    assert "unclassified" in captured


def test_cli_explicit_analysis_flag(capsys):
    exit_code = main([str(FIXTURES / "sample.jsonl"), "--analysis", "waste"])
    assert exit_code == 0
    assert "confirmed_waste" in capsys.readouterr().out


def test_cli_unknown_analysis_fails_with_dynamic_name_list(capsys):
    # argparse's own choices= validation rejects this before main()'s body
    # ever runs, so it's a SystemExit(2), not a returned exit code.
    with pytest.raises(SystemExit) as exc_info:
        main([str(FIXTURES / "sample.jsonl"), "--analysis", "bogus"])
    assert exc_info.value.code == 2
    assert "waste" in capsys.readouterr().err


def test_cli_json_output(capsys):
    exit_code = main([str(FIXTURES / "sample.jsonl"), "--format", "json"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert '"confirmed_waste"' in captured


def test_cli_strict_fails_on_malformed_input(capsys):
    exit_code = main([str(FIXTURES / "malformed.jsonl")])
    assert exit_code == 1


def test_cli_lenient_succeeds_on_malformed_input():
    exit_code = main([str(FIXTURES / "malformed.jsonl"), "--lenient"])
    assert exit_code == 0


def test_cli_html_output(capsys):
    exit_code = main([str(FIXTURES / "sample.jsonl"), "--format", "html"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "<!doctype html>" in captured.lower()


def test_cli_writes_to_output_file(tmp_path):
    out_path = tmp_path / "report.html"
    exit_code = main([str(FIXTURES / "sample.jsonl"), "--format", "html", "--output", str(out_path)])
    assert exit_code == 0
    assert out_path.exists()
    assert "<!doctype html>" in out_path.read_text(encoding="utf-8").lower()
