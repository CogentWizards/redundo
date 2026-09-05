import json
from pathlib import Path

import pytest

from redundo.adapter.otlp import OtlpParseError, parse_spans

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture():
    return json.loads((FIXTURES / "sample_otlp.json").read_text(encoding="utf-8"))


def test_parses_all_spans_across_nesting():
    spans = parse_spans(load_fixture())
    assert len(spans) == 5


def test_span_fields():
    spans = parse_spans(load_fixture())
    root = next(s for s in spans if s.span_id == "1000000000000001")
    assert root.trace_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert root.parent_span_id is None
    assert root.name == "research_agent"
    assert root.status_code == 1
    assert root.start_time_unix_nano == 1735689600000000000


def test_attribute_unwrapping():
    spans = parse_spans(load_fixture())
    llm_span = next(s for s in spans if s.span_id == "1000000000000003")
    assert llm_span.attributes["openinference.span.kind"] == "LLM"
    assert llm_span.attributes["llm.model_name"] == "gpt-5.6"
    assert llm_span.attributes["llm.token_count.prompt"] == 120
    assert isinstance(llm_span.attributes["llm.token_count.prompt"], int)


def test_parent_span_id_preserved():
    spans = parse_spans(load_fixture())
    chain_span = next(s for s in spans if s.span_id == "1000000000000002")
    assert chain_span.parent_span_id == "1000000000000001"


def test_missing_resource_spans_raises():
    with pytest.raises(OtlpParseError):
        parse_spans({"not": "otlp"})


def test_missing_trace_id_raises():
    document = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "1",
                                "name": "x",
                                "startTimeUnixNano": "1",
                                "attributes": [],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    with pytest.raises(OtlpParseError):
        parse_spans(document)
