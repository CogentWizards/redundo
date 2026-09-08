"""End-to-end tests for `redundo collect` -- real protobuf requests over a
real socket against the actual `Handler`, not mocked methods. The whole
point of this module is protocol-level plumbing (protobuf parsing, the
trace/span-id hex fixup, writing files) -- a mock would just prove the code
calls the functions it calls, not that the wire format round-trips.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from redundo.adapter import collector


@pytest.fixture()
def running_collector(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "OUT_DIR", tmp_path)
    server = ThreadingHTTPServer(("localhost", 0), collector.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], tmp_path
    finally:
        server.shutdown()
        thread.join()


def _post(port: int, path: str, body: bytes) -> int:
    conn = http.client.HTTPConnection("localhost", port)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/x-protobuf"})
        return conn.getresponse().status
    finally:
        conn.close()


def _only_file(out_dir, prefix: str):
    matches = list(out_dir.glob(f"{prefix}-*.otlp.json"))
    assert len(matches) == 1, matches
    return json.loads(matches[0].read_text(encoding="utf-8"))


def test_traces_endpoint_writes_file_with_hex_ids(running_collector):
    port, out_dir = running_collector
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = b"\x11" * 16
    span.span_id = b"\x22" * 8
    span.name = "example.event"

    status = _post(port, "/v1/traces", request.SerializeToString())

    assert status == 200
    document = _only_file(out_dir, "traces")
    written_span = document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert written_span["traceId"] == "11" * 16
    assert written_span["spanId"] == "22" * 8
    assert "parentSpanId" not in written_span


def test_logs_endpoint_writes_file_with_hex_ids(running_collector):
    port, out_dir = running_collector
    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    record.trace_id = b"\x33" * 16
    record.span_id = b"\x44" * 8

    status = _post(port, "/v1/logs", request.SerializeToString())

    assert status == 200
    document = _only_file(out_dir, "logs")
    written_record = document["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert written_record["traceId"] == "33" * 16
    assert written_record["spanId"] == "44" * 8


def test_metrics_endpoint_writes_file_and_counts_metrics(running_collector):
    port, out_dir = running_collector
    request = ExportMetricsServiceRequest()
    scope_metrics = request.resource_metrics.add().scope_metrics.add()
    scope_metrics.metrics.add(name="tokens.total")
    scope_metrics.metrics.add(name="cost.usd")

    status = _post(port, "/v1/metrics", request.SerializeToString())

    assert status == 200
    document = _only_file(out_dir, "metrics")
    written_metrics = document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert [m["name"] for m in written_metrics] == ["tokens.total", "cost.usd"]


def test_metrics_endpoint_fixes_exemplar_ids_to_hex(running_collector):
    port, out_dir = running_collector
    request = ExportMetricsServiceRequest()
    metric = request.resource_metrics.add().scope_metrics.add().metrics.add(name="gen_ai.cost")
    data_point = metric.sum.data_points.add(as_double=0.05)
    exemplar = data_point.exemplars.add(as_double=0.05)
    exemplar.trace_id = b"\x55" * 16
    exemplar.span_id = b"\x66" * 8

    status = _post(port, "/v1/metrics", request.SerializeToString())

    assert status == 200
    document = _only_file(out_dir, "metrics")
    written_exemplar = document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"][
        "dataPoints"
    ][0]["exemplars"][0]
    assert written_exemplar["traceId"] == "55" * 16
    assert written_exemplar["spanId"] == "66" * 8


def test_metrics_endpoint_leaves_summary_untouched(running_collector):
    """Summary data points have no exemplars at all -- the fixup must not
    assume every metric data type does and blow up on the ones that don't."""
    port, out_dir = running_collector
    request = ExportMetricsServiceRequest()
    metric = request.resource_metrics.add().scope_metrics.add().metrics.add(name="latency")
    metric.summary.data_points.add(count=1, sum=1.5)

    status = _post(port, "/v1/metrics", request.SerializeToString())

    assert status == 200
    document = _only_file(out_dir, "metrics")
    assert document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["name"] == "latency"


def test_unknown_path_returns_404(running_collector):
    port, _ = running_collector
    assert _post(port, "/v1/unknown", b"") == 404
