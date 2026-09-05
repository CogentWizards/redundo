"""CLI entry point: a directory of captured OTLP JSON batches -> JSONL
matching the redundo.analyzer schema contract.

    redundo adapt ./otlp_traces -o trace.jsonl

The source (OpenInference/Hermes, Claude Code, Cowork, or OpenClaw) is auto-detected
from the captured data itself -- see detect.py for exactly how, and
`--source` to skip detection and force one explicitly. A directory is
accepted rather than a single file because every source's own exporter
flushes on an interval, producing many small batch files per session
rather than one large export.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detect import DetectionError, Source, detect_source
from .otlp import OtlpParseError, is_log_document, is_trace_document
from .sources import convert_claude_code, convert_cowork, convert_openclaw, convert_openinference
from .writer import write_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redundo adapt",
        description="Convert a directory of captured OTLP JSON batches into JSONL "
        "matching the redundo analyze schema contract. The source is "
        "auto-detected from the data; pass --source to override.",
    )
    parser.add_argument(
        "otlp_dir", type=Path,
        help="Directory of *.json OTLP export batch files (traces and/or logs, any mix)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="Write JSONL here instead of stdout",
    )
    parser.add_argument(
        "--source", choices=[s.value for s in Source], default=None,
        help="Skip auto-detection and force this source",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the conversion summary (record counts, content-provenance "
        "stats, per-source notes) to stderr",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.otlp_dir.is_dir():
        print(f"redundo adapt: not a directory: {args.otlp_dir}", file=sys.stderr)
        return 1

    documents = []
    skipped_unrecognized = 0
    for path in sorted(args.otlp_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"redundo adapt: {path}: {exc}", file=sys.stderr)
            return 1
        if is_trace_document(doc) or is_log_document(doc):
            documents.append(doc)
        else:
            skipped_unrecognized += 1

    if not documents:
        print(
            f"redundo adapt: no OTLP trace or log documents found in {args.otlp_dir}",
            file=sys.stderr,
        )
        return 1

    if args.source is not None:
        source = Source(args.source)
        reason = "--source flag"
    else:
        try:
            detection = detect_source(documents)
        except DetectionError as exc:
            print(f"redundo adapt: {exc}", file=sys.stderr)
            return 1
        source, reason = detection.source, detection.reason

    trace_docs = [d for d in documents if is_trace_document(d)]
    log_docs = [d for d in documents if is_log_document(d)]

    try:
        if source is Source.OPENINFERENCE:
            records, summary = convert_openinference(trace_docs)
        elif source is Source.CLAUDE_CODE:
            records, summary = convert_claude_code(documents)
        elif source is Source.OPENCLAW:
            records, summary = convert_openclaw(trace_docs)
        else:
            records, summary = convert_cowork(log_docs)
    except OtlpParseError as exc:
        print(f"redundo adapt: {exc}", file=sys.stderr)
        return 1

    if args.output:
        with args.output.open("w", encoding="utf-8") as handle:
            write_jsonl(records, handle)
    else:
        write_jsonl(records, sys.stdout)

    if args.summary or not records:
        print(f"\nsource: {source.value} (detected via {reason})", file=sys.stderr)
        if skipped_unrecognized:
            print(
                f"  ({skipped_unrecognized} file(s) in {args.otlp_dir} were not "
                "recognized OTLP trace/log exports and were ignored)",
                file=sys.stderr,
            )
        skipped_by_kind = getattr(summary, "skipped_by_kind", None)
        if skipped_by_kind:
            for kind, count in sorted(skipped_by_kind.items()):
                print(f"  - {count} span(s)/event(s) of kind {kind!r} skipped", file=sys.stderr)
        for note in summary.notes():
            print(f"  - {note}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
