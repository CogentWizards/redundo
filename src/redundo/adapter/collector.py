"""Minimal local OTLP/HTTP receiver -- point any source's OTLP exporter at
it and it writes each POST out as OTLP JSON, one file per batch, into one
output directory that `redundo adapt` reads directly.

This is a convenience for local development and one-off analysis, not a
production observability pipeline -- if you already run a real OTel
Collector (or any backend with a file/JSON export path), point your
source at that instead and hand its output directory to `redundo adapt`
the same way. Every source this project supports needs `/v1/traces`
and/or `/v1/logs` served from the *same* endpoint (some sources use only
one, some use both, and get one endpoint config to remember either way),
so this collector always serves both, plus `/v1/metrics` for sources that
also export a metrics signal. **`redundo adapt` does not read metrics
files today** -- `/v1/metrics` exists so nothing a source sends is
silently rejected (and so metrics content is on disk for manual
inspection); a metrics-consuming analysis is a possible future addition,
not implemented yet.

Requires the `collector` extra: `pip install redundo[collector]`

Usage:
    redundo collect --port 4318 --out-dir ./otlp_traces
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
        ExportLogsServiceResponse,
    )
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
        ExportMetricsServiceRequest,
        ExportMetricsServiceResponse,
    )
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
        ExportTraceServiceResponse,
    )
except ImportError:
    print(
        "redundo collect requires the 'collector' extra: "
        "pip install redundo[collector]",
        file=sys.stderr,
    )
    raise SystemExit(1)

OUT_DIR = Path("./otlp_traces")


def _fix_span_ids_to_hex(request: ExportTraceServiceRequest, document: dict) -> None:
    """MessageToDict base64-encodes `bytes` fields by default. The OTLP
    spec carves out trace_id/span_id/parent_span_id as a documented
    exception -- those are hex in real OTLP JSON, not base64. Overwrite
    them from the original protobuf objects, which have the real bytes.
    """
    for rs_proto, rs_doc in zip(request.resource_spans, document.get("resourceSpans", [])):
        for ss_proto, ss_doc in zip(rs_proto.scope_spans, rs_doc.get("scopeSpans", [])):
            for span_proto, span_doc in zip(ss_proto.spans, ss_doc.get("spans", [])):
                span_doc["traceId"] = span_proto.trace_id.hex()
                span_doc["spanId"] = span_proto.span_id.hex()
                if span_proto.parent_span_id:
                    span_doc["parentSpanId"] = span_proto.parent_span_id.hex()
                elif "parentSpanId" in span_doc:
                    del span_doc["parentSpanId"]


def _fix_log_ids_to_hex(request: ExportLogsServiceRequest, document: dict) -> None:
    for rl_proto, rl_doc in zip(request.resource_logs, document.get("resourceLogs", [])):
        for sl_proto, sl_doc in zip(rl_proto.scope_logs, rl_doc.get("scopeLogs", [])):
            for rec_proto, rec_doc in zip(sl_proto.log_records, sl_doc.get("logRecords", [])):
                if rec_proto.trace_id:
                    rec_doc["traceId"] = rec_proto.trace_id.hex()
                elif "traceId" in rec_doc:
                    del rec_doc["traceId"]
                if rec_proto.span_id:
                    rec_doc["spanId"] = rec_proto.span_id.hex()
                elif "spanId" in rec_doc:
                    del rec_doc["spanId"]


# Metric points carry trace_id/span_id only on their exemplars, and only
# for the four data types that have exemplars at all -- Summary doesn't.
# Maps the proto oneof field name (snake_case) to its OTLP JSON key.
_METRIC_DATA_JSON_FIELD = {
    "gauge": "gauge",
    "sum": "sum",
    "histogram": "histogram",
    "exponential_histogram": "exponentialHistogram",
}


def _fix_metric_ids_to_hex(request: ExportMetricsServiceRequest, document: dict) -> None:
    for rm_proto, rm_doc in zip(request.resource_metrics, document.get("resourceMetrics", [])):
        for sm_proto, sm_doc in zip(rm_proto.scope_metrics, rm_doc.get("scopeMetrics", [])):
            for metric_proto, metric_doc in zip(sm_proto.metrics, sm_doc.get("metrics", [])):
                data_field = metric_proto.WhichOneof("data")
                json_field = _METRIC_DATA_JSON_FIELD.get(data_field)
                if json_field is None:
                    continue  # no data set, or Summary -- neither has exemplars
                data_doc = metric_doc.get(json_field, {})
                for dp_proto, dp_doc in zip(
                    getattr(metric_proto, data_field).data_points, data_doc.get("dataPoints", [])
                ):
                    for ex_proto, ex_doc in zip(dp_proto.exemplars, dp_doc.get("exemplars", [])):
                        if ex_proto.trace_id:
                            ex_doc["traceId"] = ex_proto.trace_id.hex()
                        elif "traceId" in ex_doc:
                            del ex_doc["traceId"]
                        if ex_proto.span_id:
                            ex_doc["spanId"] = ex_proto.span_id.hex()
                        elif "spanId" in ex_doc:
                            del ex_doc["spanId"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[collector] {self.address_string()} - {fmt % args}")

    def _read_body(self) -> bytes:
        """`Content-Length` is absent whenever a client sends a chunked
        request body -- some OTLP/HTTP exporters (confirmed: the JS SDK's
        `exporter-*-otlp-proto` packages) do this rather than buffering the
        whole payload first to compute a length. Reading `Content-Length`
        (defaulting to 0 when absent) silently drops the entire body in
        that case while still returning 200, which looks from the
        exporter's side like a successful export of nothing.
        """
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked_body()
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _read_chunked_body(self) -> bytes:
        chunks = []
        while True:
            size_line = self.rfile.readline()
            size = int(size_line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass  # consume trailer headers up to the final blank line
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)  # trailing CRLF after each chunk's data
        return b"".join(chunks)

    def do_POST(self):
        if self.path in ("/v1/traces", "/v1/traces/"):
            self._handle_traces()
        elif self.path in ("/v1/logs", "/v1/logs/"):
            self._handle_logs()
        elif self.path in ("/v1/metrics", "/v1/metrics/"):
            self._handle_metrics()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_traces(self):
        body = self._read_body()
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        document = MessageToDict(
            request, preserving_proto_field_name=False, use_integers_for_enums=True
        )
        _fix_span_ids_to_hex(request, document)

        span_count = sum(
            len(scope_span.get("spans", []))
            for rs in document.get("resourceSpans", [])
            for scope_span in rs.get("scopeSpans", [])
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"traces-{time.time_ns()}.otlp.json"
        out_path.write_text(json.dumps(document), encoding="utf-8")
        print(f"[collector] wrote {span_count} span(s) -> {out_path}")

        payload = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logs(self):
        body = self._read_body()
        request = ExportLogsServiceRequest()
        request.ParseFromString(body)
        document = MessageToDict(
            request, preserving_proto_field_name=False, use_integers_for_enums=True
        )
        _fix_log_ids_to_hex(request, document)

        record_count = sum(
            len(scope_log.get("logRecords", []))
            for rl in document.get("resourceLogs", [])
            for scope_log in rl.get("scopeLogs", [])
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"logs-{time.time_ns()}.otlp.json"
        out_path.write_text(json.dumps(document), encoding="utf-8")
        print(f"[collector] wrote {record_count} log record(s) -> {out_path}")

        payload = ExportLogsServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_metrics(self):
        body = self._read_body()
        request = ExportMetricsServiceRequest()
        request.ParseFromString(body)
        document = MessageToDict(
            request, preserving_proto_field_name=False, use_integers_for_enums=True
        )
        _fix_metric_ids_to_hex(request, document)

        metric_count = sum(
            len(scope_metric.get("metrics", []))
            for rm in document.get("resourceMetrics", [])
            for scope_metric in rm.get("scopeMetrics", [])
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"metrics-{time.time_ns()}.otlp.json"
        out_path.write_text(json.dumps(document), encoding="utf-8")
        print(f"[collector] wrote {metric_count} metric(s) -> {out_path}")

        payload = ExportMetricsServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redundo collect")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--out-dir", type=Path, default=Path("./otlp_traces"))
    args = parser.parse_args(argv)

    global OUT_DIR
    OUT_DIR = args.out_dir
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"redundo collect: could not create {OUT_DIR}: {exc}", file=sys.stderr)
        return 1

    try:
        server = ThreadingHTTPServer(("localhost", args.port), Handler)
    except OSError as exc:
        print(
            f"redundo collect: could not listen on port {args.port}: {exc} "
            "-- pass --port to choose a different one, or check what's already using it "
            f"(e.g. `lsof -i :{args.port}`).",
            file=sys.stderr,
        )
        return 1
    print(
        f"[collector] listening on http://localhost:{args.port}"
        "/v1/traces, /v1/logs, and /v1/metrics"
    )
    print(f"[collector] writing OTLP JSON batches to {OUT_DIR.resolve()}")
    print("[collector] Ctrl+C to stop, then: redundo adapt <out-dir> -o trace.jsonl")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
