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


def gauge_metric_document(data_points: list[dict], name: str) -> dict:
    """A minimal OTLP metrics document carrying one Gauge metric with the
    given data points -- a point-in-time value with no cumulative
    meaning, unlike cost_metric_document's Sum (see
    sources.openclaw_localtrace's own docstring for why its turn-cost
    metric is a Gauge, not a Sum). Each data point dict takes: value
    (float), time (int), attributes (dict), and optionally start_time
    (int, defaults to `time` -- a Gauge point has no real accumulation
    window, so the two are equal unless a test needs otherwise).
    """
    raw_points = []
    for dp in data_points:
        raw_points.append({
            "startTimeUnixNano": str(dp.get("start_time", dp.get("time", 0))),
            "timeUnixNano": str(dp.get("time", 0)),
            "asDouble": dp["value"],
            "attributes": [
                {"key": k, "value": _any_value(v)} for k, v in dp.get("attributes", {}).items()
            ],
        })
    return {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{
                "metrics": [{
                    "name": name,
                    "gauge": {"dataPoints": raw_points},
                }],
            }],
        }],
    }


def cost_metric_document(
    data_points: list[dict], name: str = "openclaw.cost.usd"
) -> dict:
    """A minimal OTLP metrics document carrying one Sum metric with the
    given data points. Each data point dict takes: value (float),
    start_time (int), time (int), attributes (dict), and optionally
    aggregation_temporality (int, default 2/CUMULATIVE) and is_monotonic
    (bool, default True) -- set per-call since a real point could
    legitimately differ, though every real capture seen so far agrees.
    """
    raw_points = []
    for dp in data_points:
        raw_points.append({
            "startTimeUnixNano": str(dp.get("start_time", 0)),
            "timeUnixNano": str(dp.get("time", 0)),
            "asDouble": dp["value"],
            "attributes": [
                {"key": k, "value": _any_value(v)} for k, v in dp.get("attributes", {}).items()
            ],
        })
    return {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{
                "metrics": [{
                    "name": name,
                    "sum": {
                        "dataPoints": raw_points,
                        "aggregationTemporality": 2,
                        "isMonotonic": True,
                    },
                }],
            }],
        }],
    }
