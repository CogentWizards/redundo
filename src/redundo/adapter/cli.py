"""CLI entry point: a directory of captured OTLP JSON batches, or a
source's own native capture, -> JSONL matching the redundo.analyzer
schema contract.

    redundo adapt ./otlp_traces -o trace.jsonl
    redundo adapt ~/.pi/agent/sessions/--Users-me-project--/ -o trace.jsonl

The source (OpenInference/Hermes, Claude Code, Cowork, OpenClaw, Pi, or a
third-party plugin registered under the `redundo.adapter.sources` entry
point group) is auto-detected from the captured data itself -- see
`registry.py` for exactly how, and `--source` to skip detection and force
one explicitly. For OTLP a directory is accepted rather than a single
file because every source's own exporter flushes on an interval,
producing many small batch files per session rather than one large
export.

OTLP always wins: an input is only offered to the sources' native loaders
(`AdapterSource.load()`, e.g. Pi's session JSONL) when it holds no OTLP
documents at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .base import DetectionError, InputLoadError
from .otlp import OtlpParseError, is_log_document, is_metrics_document, is_trace_document
from .registry import default_registry
from .writer import write_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redundo adapt",
        description="Convert a directory of captured OTLP JSON batches, or a "
        "source's own native capture (e.g. Pi session files), into JSONL "
        "matching the redundo analyze schema contract. The source is "
        "auto-detected from the data; pass --source to override.",
    )
    parser.add_argument(
        "input_path", type=Path, metavar="INPUT",
        help="Directory of *.json OTLP export batch files (traces, logs, and/or "
        "metrics, any mix), or a native capture: a Pi session .jsonl file, or "
        "a directory of them",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="Write JSONL here instead of stdout",
    )
    parser.add_argument(
        "--source", choices=default_registry.names(), default=None,
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

    input_path: Path = args.input_path
    if not input_path.exists():
        print(f"redundo adapt: no such file or directory: {input_path}", file=sys.stderr)
        return 1

    documents: list[dict] = []
    skipped_unrecognized = 0
    if input_path.is_dir():
        for path in sorted(input_path.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"redundo adapt: {path}: {exc}", file=sys.stderr)
                return 1
            if is_trace_document(doc) or is_log_document(doc) or is_metrics_document(doc):
                documents.append(doc)
            else:
                skipped_unrecognized += 1

    native_source: str | None = None
    if not documents:
        try:
            if args.source is not None:
                forced = default_registry.get(args.source).load(input_path)
                native = (args.source, forced) if forced else None
            else:
                native = default_registry.load_native(input_path)
        except InputLoadError as exc:
            print(f"redundo adapt: {exc}", file=sys.stderr)
            return 1
        if native is not None:
            native_source, documents = native
            skipped_unrecognized = 0

    if not documents:
        if input_path.is_dir():
            print(
                f"redundo adapt: no OTLP trace, log, or metrics documents found in "
                f"{input_path}, and no source recognized it as its own native format",
                file=sys.stderr,
            )
        else:
            print(
                f"redundo adapt: {input_path} is a file, but no source recognized it "
                "as its own native format (OTLP input must be a directory of *.json "
                "batch files)",
                file=sys.stderr,
            )
        return 1

    if args.source is not None:
        source_name, reason = args.source, "--source flag"
    elif native_source is not None:
        source_name, reason = native_source, f"{native_source} source's own native loader"
    else:
        try:
            detection = default_registry.detect(documents)
        except DetectionError as exc:
            print(f"redundo adapt: {exc}", file=sys.stderr)
            return 1
        source_name, reason = detection.source, detection.reason

    try:
        source = default_registry.get(source_name)
        records, summary = source.convert(documents)
    except OtlpParseError as exc:
        print(f"redundo adapt: {exc}", file=sys.stderr)
        return 1

    # Stamped here, once, corpus-wide -- not by any individual adapter,
    # which has no reason to know or care where its own input documents
    # came from. This is what lets a report later point a reader back at
    # the raw capture behind a specific sample case. See schema.py's
    # META_SOURCE_PATH_KEY for the privacy note: a local path is real
    # information about the machine that produced it.
    resolved_source = str(input_path.resolve())
    for record in records:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["source_path"] = resolved_source
        record["metadata"] = metadata

    if args.output:
        try:
            with args.output.open("w", encoding="utf-8") as handle:
                write_jsonl(records, handle)
        except OSError as exc:
            print(f"redundo adapt: could not write {args.output}: {exc}", file=sys.stderr)
            return 1
    else:
        write_jsonl(records, sys.stdout)

    if args.summary or not records:
        print(f"\nsource: {source_name} (detected via {reason})", file=sys.stderr)
        if skipped_unrecognized:
            print(
                f"  ({skipped_unrecognized} file(s) in {input_path} were not "
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
