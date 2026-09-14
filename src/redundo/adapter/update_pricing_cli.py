"""`redundo update-pricing` fetches a fresh pricing snapshot from
LiteLLM's public data and writes it as a local override file, so anyone
with only the published package installed can refresh pricing without
waiting for a new release. See pricing.py's module docstring for how this
override layers over the bundled table at runtime.

The only place in redundo's normal operation that makes an outbound
network call, and only when a user runs this command by hand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import pricing, pricing_fetch

USAGE = """\
usage: redundo update-pricing [--out PATH]

Fetches a curated snapshot of LiteLLM's public pricing data and writes it
as redundo's pricing override (defaults to {default_path}).

  --out PATH  Write the pricing table to a custom location instead of the
              default override path redundo reads from automatically.
"""


def main(argv: list[str]) -> int:
    out_path = pricing.default_override_path()
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            print(USAGE.format(default_path=pricing.default_override_path()))
            return 0
        if arg == "--out":
            if not args:
                print("redundo update-pricing: --out requires a path", file=sys.stderr)
                return 1
            out_path = Path(args.pop(0)).expanduser()
            continue
        print(f"redundo update-pricing: unrecognized argument {arg!r}", file=sys.stderr)
        print(USAGE.format(default_path=pricing.default_override_path()), file=sys.stderr)
        return 1

    print(f"Fetching pricing data from {pricing_fetch.SOURCE_URL} ...")
    try:
        raw = pricing_fetch.fetch_litellm_pricing_data()
    except Exception as error:  # network/HTTP/JSON failures all land here
        print(f"redundo update-pricing failed: {error}", file=sys.stderr)
        return 1

    table = pricing_fetch.build_pricing_table_file(raw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"Wrote {len(table['entries'])} entries to {out_path} "
        f"(generated_at: {table['generated_at']})"
    )
    if out_path != pricing.default_override_path():
        print(
            f"Custom output path: redundo only reads {pricing.default_override_path()} "
            "by default. Move the file there, or point your own tooling at "
            f"{out_path} directly."
        )
    return 0
