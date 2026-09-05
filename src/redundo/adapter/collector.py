"""Minimal local OTLP/HTTP receiver -- point any source's OTLP exporter at
it and it writes each POST out as OTLP JSON, one file per batch, into one
output directory that `redundo adapt` reads directly.

This is a convenience for local development and one-off analysis, not a
production observability pipeline -- if you already run a real OTel
Collector (or any backend with a file/JSON export path), point your
source at that instead and hand its output directory to `redundo adapt`
the same way. Every source this project supports needs both `/v1/traces`
and `/v1/logs` served from the *same* endpoint (some sources use only one,
some use both, and get one endpoint config to remember either way), so
this collector always serves both.

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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[collector] {self.address_string()} - {fmt % args}")

    def do_POST(self):
        if self.path in ("/v1/traces", "/v1/traces/"):
            self._handle_traces()
        elif self.path in ("/v1/logs", "/v1/logs/"):
            self._handle_logs()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_traces(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
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
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redundo collect")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--out-dir", type=Path, default=Path("./otlp_traces"))
    args = parser.parse_args(argv)

    global OUT_DIR
    OUT_DIR = args.out_dir
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("localhost", args.port), Handler)
    print(f"[collector] listening on http://localhost:{args.port}/v1/traces and /v1/logs")
    print(f"[collector] writing OTLP JSON batches to {OUT_DIR.resolve()}")
    print("[collector] Ctrl+C to stop, then: redundo adapt <out-dir> -o trace.jsonl")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
