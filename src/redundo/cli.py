"""Top-level `redundo` entry point: dispatches to one of three
subcommands. `adapt` and `analyze` are the core pipeline and have zero
dependencies; `collect` is a convenience local OTLP receiver gated behind
the `collector` extra, imported lazily so installing redundo for just
`adapt`/`analyze` never pulls in protobuf/opentelemetry-proto.

    redundo adapt --source openinference traces/ | redundo analyze > report.html
"""

from __future__ import annotations

import sys

USAGE = """\
redundo: adapt agent traces to a common schema, then analyze them.

    redundo adapt --source openinference traces/ | redundo analyze > report.html

Subcommands:
  adapt     Convert captured OTLP traces into the schema (redundo adapt -h)
  analyze   Classify redundant calls in a schema trace   (redundo analyze -h)
  collect   Run a local OTLP receiver for capture         (redundo collect -h)
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

    print(f"redundo: unknown subcommand {command!r} (expected adapt, analyze, or collect)\n",
          file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
