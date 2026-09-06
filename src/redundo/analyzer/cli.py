"""CLI entry point: read a normalized JSONL trace (file or stdin), print an
analysis report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ingest import IngestError, load_events
from .registry import default_registry
from .report import to_html, to_json, to_text

_RENDERERS = {"text": to_text, "json": to_json, "html": to_html}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redundo analyze",
        description="Run an analysis over a normalized trace -- by default, "
        "classify repeated LLM/tool calls as confirmed waste, likely "
        "legitimate, or unclassified.",
    )
    parser.add_argument(
        "trace", type=str, nargs="?", default="-",
        help="Path to a JSONL file matching the schema contract. Omit, or pass "
        "'-', to read from stdin -- e.g. `redundo adapt ... | redundo analyze`.",
    )
    parser.add_argument(
        "--analysis",
        choices=default_registry.names(),
        default="waste",
        help="Which analysis to run (default: waste)",
    )
    parser.add_argument(
        "--format",
        choices=tuple(_RENDERERS),
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

    try:
        analysis = default_registry.get(args.analysis, keep_reasons=args.samples)
    except TypeError:
        # This analysis's constructor doesn't accept keep_reasons -- not
        # every analysis needs a "how many samples to keep" knob.
        analysis = default_registry.get(args.analysis)
    result = analysis.run(events)

    render = _RENDERERS[args.format]
    output = render(result, max_reasons=args.samples)

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
