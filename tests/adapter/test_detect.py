import pytest

from redundo.adapter.detect import DetectionError, detect_source
from helpers import log_record, logs_document, span, traces_document


def test_claude_code_span_name_detected():
    doc = traces_document([span("s1", name="claude_code.llm_request", start=0)])
    detection = detect_source([doc])
    assert detection.source == "claude-code"
    assert "span name" in detection.reason


def test_openinference_span_kind_detected():
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    detection = detect_source([doc])
    assert detection.source == "openinference"


def test_openclaw_span_name_detected():
    doc = traces_document([span("s1", name="openclaw.model.call", start=0)])
    detection = detect_source([doc])
    assert detection.source == "openclaw"
    assert "span name" in detection.reason


def test_openclaw_observation_unit_attribute_detected_under_latest_semconv_naming():
    # Under OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental, the
    # span is named "<operation> <model>" instead of "openclaw.model.call"
    # -- openclaw.model_call.observation_unit is the fallback signal.
    doc = traces_document([
        span("s1", name="chat gpt-5.4", start=0,
             attributes={"openclaw.model_call.observation_unit": "request"}),
    ])
    detection = detect_source([doc])
    assert detection.source == "openclaw"
    assert "observation_unit" in detection.reason


def test_claude_code_wins_over_openinference_if_both_present_in_one_corpus():
    # Shouldn't happen in practice, but claude_code.* span names are
    # checked first -- verifies the ordering is deterministic, not that
    # mixing sources is a supported/expected scenario.
    doc = traces_document([
        span("s1", name="claude_code.llm_request", start=0),
        span("s2", start=1, attributes={"openinference.span.kind": "LLM"}),
    ])
    detection = detect_source([doc])
    assert detection.source == "claude-code"


def test_logs_only_claude_code_detected_via_service_name():
    doc = logs_document(
        [log_record(attributes={"event.name": "api_request", "session.id": "s"})],
        resource_attributes={"service.name": "claude-code"},
    )
    detection = detect_source([doc])
    assert detection.source == "claude-code"
    assert "service.name" in detection.reason


def test_logs_only_cowork_detected_via_service_name():
    doc = logs_document(
        [log_record(attributes={"event.name": "api_request", "session.id": "s"})],
        resource_attributes={"service.name": "cowork"},
    )
    detection = detect_source([doc])
    assert detection.source == "cowork"
    assert "service.name" in detection.reason


def test_logs_only_falls_back_to_claude_code_only_event_names():
    # No service.name at all -- mcp_server_connection is never in Cowork's
    # documented event list, so its presence alone should be enough.
    doc = logs_document([
        log_record(attributes={"event.name": "api_request", "session.id": "s"}),
        log_record(attributes={"event.name": "mcp_server_connection", "session.id": "s"}),
    ])
    detection = detect_source([doc])
    assert detection.source == "claude-code"
    assert "not in Cowork" in detection.reason


def test_logs_only_falls_back_to_cowork_when_event_names_are_a_subset():
    # No service.name, and every event name seen is one Cowork documents
    # -- genuinely ambiguous in principle, but this is the honest
    # last-resort default given no Claude-Code-only signal fired.
    doc = logs_document([
        log_record(attributes={"event.name": "api_request", "session.id": "s"}),
        log_record(attributes={"event.name": "tool_result", "session.id": "s"}),
    ])
    detection = detect_source([doc])
    assert detection.source == "cowork"


def test_detection_fails_loudly_on_unrecognizable_corpus():
    doc = traces_document([span("s1", name="something.unrelated", start=0)])
    with pytest.raises(DetectionError, match="--source"):
        detect_source([doc])


def test_detection_fails_on_empty_corpus():
    with pytest.raises(DetectionError):
        detect_source([])
