"""`redundo drift`: prints context-drift hints, deliberately separate from
`redundo analyze`. See context_drift.py's module docstring for why: this
is a heuristic with no real-data calibration behind it yet, not a
finding, and it doesn't fit AnalysisResult's cost/token-centric Bucket
shape at all. Kept as its own command, its own output format, so nobody
gets this mixed into a waste report they might trust or forward.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context_drift import DriftHint, find_continuation_chains, find_drift_hints
from .ingest import IngestError, IngestIOError, load_events
from .task_graph import build_task_graph

_DISCLAIMER = (
    "Context drift hints (a heuristic, not a finding)\n"
    "\n"
    "No redundo adapter currently emits a real \"continuation\" link (see\n"
    "docs/context-drift.md), so unlike this project's other thresholds,\n"
    "nothing below has ever been checked against real drift data. Every\n"
    "number here is a place to look, not a verdict.\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redundo drift",
        description="Print cross-run context-drift hints for continuation "
        "chains in a normalized trace. A heuristic signal, not a finding, "
        "see docs/context-drift.md.",
    )
    parser.add_argument(
        "trace", type=str, nargs="?", default="-",
        help="Path to a JSONL file matching the schema contract. Omit, or pass "
        "'-', to read from stdin.",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="Write output to this file instead of stdout",
    )
    parser.add_argument(
        "--lenient", action="store_true",
        help="Skip malformed rows instead of failing on the first one",
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
        print(f"redundo drift: {exc}", file=sys.stderr)
        if not isinstance(exc, IngestIOError):
            print("Pass --lenient to skip malformed rows instead of failing.", file=sys.stderr)
        return 1

    if not events:
        print("redundo drift: no events loaded", file=sys.stderr)
        return 1

    graph = build_task_graph(events)
    chains = find_continuation_chains(graph)
    hints = find_drift_hints(events, graph=graph)
    hints_by_hop = {(h.predecessor_task_id, h.successor_task_id): h for h in hints}

    output = (
        _to_json(chains, hints_by_hop) if args.format == "json" else _to_text(chains, hints_by_hop)
    )

    if args.output:
        try:
            args.output.write_text(output, encoding="utf-8")
        except OSError as exc:
            print(f"redundo drift: could not write {args.output}: {exc}", file=sys.stderr)
            return 1
    else:
        print(output)
    return 0


def _to_text(chains: list[list[str]], hints_by_hop: dict[tuple[str, str], DriftHint]) -> str:
    if not chains:
        return (
            _DISCLAIMER
            + "\nNo continuation chains found: every source in this corpus either "
            "reports no parent_task_id at all, or only delegation-kind links "
            "(see task_graph.py). Nothing to compare."
        )

    lines = [_DISCLAIMER]
    for chain in chains:
        lines.append(f"\nChain: {' -> '.join(chain)} ({len(chain)} tasks)")
        for predecessor_id, successor_id in zip(chain, chain[1:]):
            hint = hints_by_hop[(predecessor_id, successor_id)]
            lines.append(f"\n  {predecessor_id} -> {successor_id}:")
            lines.append(
                f"    shape distance: {hint.shape_distance} step(s) changed "
                f"({hint.shape_distance_fraction:.0%} of "
                f"{max(hint.predecessor_call_count, hint.successor_call_count)})"
            )
            if hint.content_distances:
                worst = max(hint.content_distances, key=lambda t: t[2])
                lines.append(
                    f"    content distance at matched steps: worst "
                    f"{worst[2]}/64 bits (step {worst[0]} -> step {worst[1]})"
                )
            for event_type, name, step in hint.added:
                lines.append(f"    + added: {event_type}/{name} (step {step})")
            for event_type, name, step in hint.removed:
                lines.append(f"    - removed: {event_type}/{name} (step {step})")
            for (pred_type, pred_name, pred_step), (succ_type, succ_name, succ_step) in hint.substituted:
                lines.append(
                    f"    ~ substituted: {pred_type}/{pred_name} (step {pred_step}) -> "
                    f"{succ_type}/{succ_name} (step {succ_step})"
                )
    return "\n".join(lines)


def _hint_to_dict(h: DriftHint) -> dict:
    return {
        "predecessor_task_id": h.predecessor_task_id,
        "successor_task_id": h.successor_task_id,
        "predecessor_call_count": h.predecessor_call_count,
        "successor_call_count": h.successor_call_count,
        "shape_distance": h.shape_distance,
        "shape_distance_fraction": round(h.shape_distance_fraction, 4),
        "content_distances": [
            {"predecessor_step": p, "successor_step": s, "hamming_distance": d}
            for p, s, d in h.content_distances
        ],
        "added": [{"event_type": t, "name": n, "step_index": s} for t, n, s in h.added],
        "removed": [{"event_type": t, "name": n, "step_index": s} for t, n, s in h.removed],
        "substituted": [
            {
                "predecessor": {"event_type": pt, "name": pn, "step_index": ps},
                "successor": {"event_type": st, "name": sn, "step_index": ss},
            }
            for (pt, pn, ps), (st, sn, ss) in h.substituted
        ],
    }


def _to_json(chains: list[list[str]], hints_by_hop: dict[tuple[str, str], DriftHint]) -> str:
    return json.dumps({
        "disclaimer": (
            "A heuristic, not a finding. No redundo adapter currently emits a "
            "real continuation link, so nothing here has ever been checked "
            "against real drift data."
        ),
        "chains": [
            {
                "tasks": chain,
                "hops": [
                    _hint_to_dict(hints_by_hop[(predecessor_id, successor_id)])
                    for predecessor_id, successor_id in zip(chain, chain[1:])
                ],
            }
            for chain in chains
        ],
    }, indent=2)
