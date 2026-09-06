"""redundo.analyzer: run analyses over a normalized trace.

Public surface:
    schema.Event                        the contract
    ingest.load_events                  JSONL -> list[Event]
    analysis.Analysis                   the contract every analysis implements
    analysis.AnalysisResult             what every analysis produces
    analyses.WasteAnalysis              the one analysis this project ships with
    registry.AnalysisRegistry           discovers analyses (built-in + plugins)
    report.to_text / to_json / to_html  render an AnalysisResult

    classify.classify_pair, cycles.find_candidate_pairs, CandidatePair,
    Verdict -- WasteAnalysis's own internals, still public for anyone
    building a different analysis on the same candidate-pair machinery.
"""

from .analyses import RULE_TEXT, WasteAnalysis
from .analysis import Analysis, AnalysisResult, Bucket
from .classify import Classification, Verdict, classify_pair
from .cycles import CandidatePair, find_candidate_pairs
from .ingest import IngestError, check_consistent_hash_spec, load_events
from .metrics import CoverageStats, Slice, compute_generic_coverage
from .registry import AnalysisRegistry
from .report import to_html, to_json, to_text
from .schema import Event, SchemaError

__all__ = [
    "RULE_TEXT",
    "Analysis",
    "AnalysisRegistry",
    "AnalysisResult",
    "Bucket",
    "CandidatePair",
    "Classification",
    "CoverageStats",
    "Event",
    "IngestError",
    "SchemaError",
    "Slice",
    "Verdict",
    "WasteAnalysis",
    "check_consistent_hash_spec",
    "classify_pair",
    "compute_generic_coverage",
    "find_candidate_pairs",
    "load_events",
    "to_html",
    "to_json",
    "to_text",
]
