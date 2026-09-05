"""Parse OTLP JSON export into flat span/log records.

Shared by every source-specific converter in `redundo.adapter.sources` --
this module knows nothing about any particular agent framework's span
names or attribute semantics, only the OTLP JSON wire shape itself:

- traces (resourceSpans -> scopeSpans -> spans): the span tree. Spans can
  carry `events`: timestamped sub-records nested inside a span (a genuine,
  distinct OTLP concept from span attributes -- some sources put real
  content there instead of in attributes; a reader that only looks at
  `attributes` misses it entirely).
- logs (resourceLogs -> scopeLogs -> logRecords): structured events.
  Several sources put data here that never appears on the traces signal
  at all -- see each source's own docs/*.md for specifics.

Every span and log record also carries `resource_attributes`: the
OTLP `resource.attributes` block attached once per `resourceSpans`/
`resourceLogs` entry, describing the *emitting process* (`service.name`,
`service.version`, host/OS info, ...) rather than the individual
span/record. This is what `redundo.adapter.detect` uses to tell two
sources apart when their event/span shapes alone are ambiguous (see
detect.py for why that ambiguity is real, not hypothetical).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OtlpParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    time_unix_nano: int
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int | None
    status_code: int | None  # OTLP StatusCode: 0=UNSET, 1=OK, 2=ERROR
    attributes: dict[str, Any] = field(default_factory=dict)
    events: tuple[SpanEvent, ...] = field(default_factory=tuple)
    resource_attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LogRecord:
    time_unix_nano: int
    trace_id: str | None
    span_id: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    resource_attributes: dict[str, Any] = field(default_factory=dict)


def _attr_value(value: dict[str, Any]) -> Any:
    """Unwrap an OTLP AnyValue. Only the scalar kinds actually observed in
    practice are handled; arrays/kvlists pass through as their raw
    dict/list so callers can still inspect them if needed.
    """
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        # OTLP JSON stringifies int64 to dodge JS float precision loss.
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [_attr_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            kv["key"]: _attr_value(kv["value"])
            for kv in value["kvlistValue"].get("values", [])
            if "value" in kv
        }
    return None


def _attrs_to_dict(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in attributes:
        key = attr.get("key")
        if key is None or "value" not in attr:
            continue
        out[key] = _attr_value(attr["value"])
    return out


def is_trace_document(document: dict[str, Any]) -> bool:
    return "resourceSpans" in document


def is_log_document(document: dict[str, Any]) -> bool:
    return "resourceLogs" in document


def document_resource_attributes(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every resource.attributes block in this document, one dict per
    resourceSpans/resourceLogs entry (a single document can in principle
    carry more than one, though in practice every exporter observed so
    far emits exactly one per document). Used by detect.py to sniff
    `service.name` without needing a full parse_spans()/parse_log_records()
    pass first.
    """
    out = []
    for key in ("resourceSpans", "resourceLogs"):
        for entry in document.get(key, []):
            resource = entry.get("resource") or {}
            out.append(_attrs_to_dict(resource.get("attributes", [])))
    return out


def parse_spans(document: dict[str, Any]) -> list[Span]:
    """Flatten one OTLP traces JSON export document into Span records, in
    file order (not time order -- each converter sorts by start time
    within each task itself).
    """
    if not is_trace_document(document):
        raise OtlpParseError("not an OTLP traces export: missing 'resourceSpans'")

    spans: list[Span] = []
    for resource_span in document.get("resourceSpans", []):
        resource_attrs = _attrs_to_dict((resource_span.get("resource") or {}).get("attributes", []))
        for scope_span in resource_span.get("scopeSpans", []):
            for raw_span in scope_span.get("spans", []):
                spans.append(_parse_one_span(raw_span, resource_attrs))
    return spans


def _parse_one_span(raw: dict[str, Any], resource_attrs: dict[str, Any]) -> Span:
    trace_id = raw.get("traceId")
    span_id = raw.get("spanId")
    if not trace_id or not span_id:
        raise OtlpParseError(f"span missing traceId or spanId: {raw.get('name')!r}")

    parent_span_id = raw.get("parentSpanId") or None
    status = raw.get("status") or {}

    try:
        start = int(raw["startTimeUnixNano"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OtlpParseError(f"span {span_id!r} missing/invalid startTimeUnixNano") from exc

    end_raw = raw.get("endTimeUnixNano")
    end = int(end_raw) if end_raw not in (None, "") else None

    events = tuple(
        SpanEvent(
            name=ev.get("name", ""),
            time_unix_nano=int(ev.get("timeUnixNano") or 0),
            attributes=_attrs_to_dict(ev.get("attributes", [])),
        )
        for ev in raw.get("events", [])
    )

    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=raw.get("name", ""),
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        status_code=status.get("code"),
        attributes=_attrs_to_dict(raw.get("attributes", [])),
        events=events,
        resource_attributes=resource_attrs,
    )


def parse_log_records(document: dict[str, Any]) -> list[LogRecord]:
    """Flatten one OTLP logs JSON export document into LogRecord records."""
    if not is_log_document(document):
        raise OtlpParseError("not an OTLP logs export: missing 'resourceLogs'")

    records: list[LogRecord] = []
    for resource_log in document.get("resourceLogs", []):
        resource_attrs = _attrs_to_dict((resource_log.get("resource") or {}).get("attributes", []))
        for scope_log in resource_log.get("scopeLogs", []):
            for raw_record in scope_log.get("logRecords", []):
                records.append(_parse_one_log_record(raw_record, resource_attrs))
    return records


def _parse_one_log_record(raw: dict[str, Any], resource_attrs: dict[str, Any]) -> LogRecord:
    body_raw = raw.get("body")
    body = _attr_value(body_raw) if isinstance(body_raw, dict) else body_raw
    time_raw = raw.get("timeUnixNano") or raw.get("observedTimeUnixNano") or 0
    return LogRecord(
        time_unix_nano=int(time_raw),
        trace_id=raw.get("traceId") or None,
        span_id=raw.get("spanId") or None,
        attributes=_attrs_to_dict(raw.get("attributes", [])),
        body=body,
        resource_attributes=resource_attrs,
    )
