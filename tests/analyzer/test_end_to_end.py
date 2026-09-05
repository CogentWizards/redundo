from pathlib import Path

from redundo.analyzer.classify import Verdict, classify_pair
from redundo.analyzer.cli import main
from redundo.analyzer.cycles import find_candidate_pairs
from redundo.analyzer.ingest import load_events
from redundo.analyzer.lineage import group_by_task
from redundo.analyzer.metrics import build_report

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

    report = build_report(classifications, events)
    assert report.total_candidate_pairs == 5
    assert report.by_verdict[Verdict.CONFIRMED_WASTE].count == 1
    assert report.by_verdict[Verdict.LIKELY_LEGITIMATE].count == 3
    assert report.by_verdict[Verdict.UNCLASSIFIED].count == 1


def test_cli_text_output(capsys):
    exit_code = main([str(FIXTURES / "sample.jsonl")])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "confirmed_waste" in captured
    assert "likely_legitimate" in captured
    assert "unclassified" in captured


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
