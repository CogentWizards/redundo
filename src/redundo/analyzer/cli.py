"""CLI entry point: read a normalized JSONL trace (file or stdin), print a
waste report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .classify import classify_pair
from .cycles import find_candidate_pairs
from .ingest import IngestError, load_events
from .lineage import group_by_task
from .metrics import build_report
from .report import to_html, to_json, to_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redundo analyze",
        description="Classify repeated LLM/tool calls in a normalized trace as "
        "confirmed waste, likely legitimate, or unclassified.",
    )
    parser.add_argument(
        "trace", type=str, nargs="?", default="-",
        help="Path to a JSONL file matching the schema contract. Omit, or pass "
        "'-', to read from stdin -- e.g. `redundo adapt ... | redundo analyze`.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "html"),
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write output to this file instead of stdout",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="Skip malformed rows instead of failing on the first one",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=20,
        metavar="N",
        help="Number of example cases to keep per bucket for spot-checking (default: 20)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    source = sys.stdin if args.trace == "-" else Path(args.trace)

    errors: list[str] = []
    try:
        events = load_events(source, strict=not args.lenient, on_error=errors)
    except IngestError as exc:
        print(f"redundo analyze: {exc}", file=sys.stderr)
        print("Pass --lenient to skip malformed rows instead of failing.", file=sys.stderr)
        return 1

    if not events:
        print("redundo analyze: no events loaded", file=sys.stderr)
        return 1

    lineages = group_by_task(events)
    pairs = find_candidate_pairs(events)
    classifications = [classify_pair(pair, lineages[pair.task_id]) for pair in pairs]
    report = build_report(classifications, events, keep_reasons=args.samples)

    if args.format == "json":
        output = to_json(report)
    elif args.format == "html":
        output = to_html(report)
    else:
        output = to_text(report)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Wrote {args.format} report to {args.output}", file=sys.stderr)
    else:
        print(output)

    if errors:
        print(f"\n({len(errors)} row(s) skipped, see stderr with --lenient)", file=sys.stderr)
        for error in errors[:10]:
            print(f"  {error}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
