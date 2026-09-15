"""Top-level `redundo` entry point: dispatches to one of five
subcommands. `adapt` and `analyze` are the core pipeline and have zero
dependencies; `collect` is a convenience local OTLP receiver gated behind
the `collector` extra, imported lazily so installing redundo for just
`adapt`/`analyze` never pulls in protobuf/opentelemetry-proto.
`update-pricing` is the one command that makes a network call, and only
when a user runs it by hand; see pricing.py's module docstring. `drift`
is a heuristic hint, deliberately separate from `analyze`'s own report,
see context_drift.py's module docstring for why.

    redundo adapt --source openinference traces/ | redundo analyze > report.html
"""

from __future__ import annotations

import sys

USAGE = """\
redundo: adapt agent traces to a common schema, then analyze them.

    redundo adapt --source openinference traces/ | redundo analyze > report.html

Subcommands:
  adapt           Convert captured OTLP traces into the schema (redundo adapt -h)
  analyze         Classify redundant calls in a schema trace   (redundo analyze -h)
  collect         Run a local OTLP receiver for capture         (redundo collect -h)
  update-pricing  Refresh the bundled per-model pricing table   (redundo update-pricing -h)
  drift           Cross-run context-drift hints (heuristic)     (redundo drift -h)
"""


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not argv:
        print(USAGE, file=sys.stderr)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    command, rest = argv[0], argv[1:]

    if command == "adapt":
        from .adapter.cli import main as adapt_main

        return adapt_main(rest)
    if command == "analyze":
        from .analyzer.cli import main as analyze_main

        return analyze_main(rest)
    if command == "collect":
        from .adapter.collector import main as collect_main

        return collect_main(rest)
    if command == "update-pricing":
        from .adapter.update_pricing_cli import main as update_pricing_main

        return update_pricing_main(rest)
    if command == "drift":
        from .analyzer.drift_cli import main as drift_main

        return drift_main(rest)

    print(
        f"redundo: unknown subcommand {command!r} "
        "(expected adapt, analyze, collect, update-pricing, or drift)\n",
        file=sys.stderr,
    )
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
