"""A minimal AdapterSource: recognizes one made-up span name
(`example.event`) and converts it into one `tool_call` record per span.

Real sources (see redundo's own `src/redundo/adapter/sources/`) do a lot
more -- lineage, content hashing, multi-signal joins. This one skips all
of that on purpose: the point is showing the *shape* of the contract,
not a second real integration.
"""

from __future__ import annotations

from typing import Any

from redundo.adapter import AdapterSource, Detection
from redundo.adapter.otlp import is_trace_document, parse_spans

_MARKER_SPAN_NAME = "example.event"


class ExampleConversionSummary:
    def __init__(self, record_count: int) -> None:
        self.record_count = record_count

    def notes(self) -> list[str]:
        return [f"example plugin: converted {self.record_count} record(s)."]


class ExampleSource(AdapterSource):
    name = "example"

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if not is_trace_document(doc):
                continue
            for span in parse_spans(doc):
                if span.name == _MARKER_SPAN_NAME:
                    return Detection("example", f"span name {span.name!r}")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        trace_docs = [d for d in documents if is_trace_document(d)]
        records: list[dict[str, Any]] = []
        for doc in trace_docs:
            for span in parse_spans(doc):
                if span.name != _MARKER_SPAN_NAME:
                    continue
                records.append({
                    "task_id": span.trace_id,
                    "step_index": len(records),
                    "event_type": "tool_call",
                    "name": "example_tool",
                    "content_hash": span.attributes.get("content_hash", "0" * 16),
                    "tokens_in": None,
                    "tokens_out": None,
                    "outcome": None,
                    "timestamp": None,
                    "cost_usd": None,
                    "model": None,
                    "parent_id": None,
                    "workflow": None,
                    "metadata": {},
                })
        return records, ExampleConversionSummary(len(records))
