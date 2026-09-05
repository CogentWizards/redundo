"""redundo.analyzer: classify repeated LLM/tool calls in a normalized trace.

Public surface:
    schema.Event                        the contract
    ingest.load_events                  JSONL -> list[Event]
    cycles.find_candidate_pairs         detect redundant-repeat candidates
    classify.classify_pair              one of confirmed_waste / likely_legitimate / unclassified
    metrics.build_report                aggregate counts + cost/tokens by bucket
    report.to_text / to_json / to_html  render a Report
"""

from .classify import Classification, Verdict, classify_pair
from .cycles import CandidatePair, find_candidate_pairs
from .ingest import IngestError, check_consistent_hash_spec, load_events
from .metrics import CoverageStats, Report, build_report
from .report import RULE_TEXT, to_html, to_json, to_text
from .schema import Event, SchemaError

__all__ = [
    "RULE_TEXT",
    "CandidatePair",
    "Classification",
    "CoverageStats",
    "Event",
    "IngestError",
    "Report",
    "SchemaError",
    "Verdict",
    "build_report",
    "check_consistent_hash_spec",
    "classify_pair",
    "find_candidate_pairs",
    "load_events",
    "to_html",
    "to_json",
    "to_text",
]
