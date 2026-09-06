"""A trivial, test-only AdapterSource -- proves entry-point discovery finds
a genuinely third-party, separately-installed package, not just something
monkeypatched in-process. Recognizes one made-up span name and produces
one made-up record; nothing about it is meant to be realistic telemetry.
"""

from __future__ import annotations

from typing import Any

from redundo.adapter import AdapterSource, Detection
from redundo.adapter.otlp import is_trace_document, parse_spans


class _Summary:
    def notes(self) -> list[str]:
        return ["dummy plugin: this is a test fixture, not a real source."]


class DummySource(AdapterSource):
    name = "dummy"

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if not is_trace_document(doc):
                continue
            for span in parse_spans(doc):
                if span.name == "dummy.marker":
                    return Detection("dummy", "span name 'dummy.marker'")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        trace_docs = [d for d in documents if is_trace_document(d)]
        records = []
        for doc in trace_docs:
            for span in parse_spans(doc):
                if span.name == "dummy.marker":
                    records.append({
                        "task_id": span.trace_id,
                        "step_index": 0,
                        "event_type": "tool_call",
                        "name": "dummy",
                        "content_hash": "0" * 16,
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
        return records, _Summary()
