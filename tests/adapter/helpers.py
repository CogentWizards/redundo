"""Shared fixture builders: hand-built Span/LogRecord objects, wrapped back
into OTLP-shaped documents so the converters (which parse them internally)
can consume them.
"""
from __future__ import annotations

from redundo.adapter.otlp import LogRecord, Span, SpanEvent


def _any_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def span(
    span_id,
    trace_id="t1",
    parent_span_id=None,
    name="span",
    start=0,
    end=None,
    status_code=None,
    attributes=None,
    events=None,
):
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        status_code=status_code,
        attributes=attributes or {},
        events=tuple(events or ()),
    )


def span_event(name, time=0, attributes=None):
    return SpanEvent(name=name, time_unix_nano=time, attributes=attributes or {})


def traces_document(spans: list[Span], resource_attributes: dict | None = None) -> dict:
    raw_spans = []
    for s in spans:
        raw = {
            "traceId": s.trace_id,
            "spanId": s.span_id,
            "name": s.name,
            "startTimeUnixNano": str(s.start_time_unix_nano),
            "attributes": [{"key": k, "value": _any_value(v)} for k, v in s.attributes.items()],
        }
        if s.parent_span_id:
            raw["parentSpanId"] = s.parent_span_id
        if s.end_time_unix_nano is not None:
            raw["endTimeUnixNano"] = str(s.end_time_unix_nano)
        if s.status_code is not None:
            raw["status"] = {"code": s.status_code}
        if s.events:
            raw["events"] = [
                {
                    "name": ev.name,
                    "timeUnixNano": str(ev.time_unix_nano),
                    "attributes": [
                        {"key": k, "value": _any_value(v)} for k, v in ev.attributes.items()
                    ],
                }
                for ev in s.events
            ]
        raw_spans.append(raw)
    resource = {
        "attributes": [
            {"key": k, "value": _any_value(v)} for k, v in (resource_attributes or {}).items()
        ]
    }
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": raw_spans}]}]}


def log_record(attributes=None, body=None, trace_id=None, span_id=None, time=0):
    return LogRecord(
        time_unix_nano=time,
        trace_id=trace_id,
        span_id=span_id,
        attributes=attributes or {},
        body=body,
    )


def logs_document(records: list[LogRecord], resource_attributes: dict | None = None) -> dict:
    raw_records = []
    for r in records:
        raw = {
            "timeUnixNano": str(r.time_unix_nano),
            "attributes": [{"key": k, "value": _any_value(v)} for k, v in r.attributes.items()],
        }
        if r.trace_id:
            raw["traceId"] = r.trace_id
        if r.span_id:
            raw["spanId"] = r.span_id
        if r.body is not None:
            raw["body"] = _any_value(r.body)
        raw_records.append(raw)
    resource = {
        "attributes": [
            {"key": k, "value": _any_value(v)} for k, v in (resource_attributes or {}).items()
        ]
    }
    return {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": raw_records}]}]}
