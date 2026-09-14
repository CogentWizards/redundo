#!/usr/bin/env python3
"""Regenerates src/redundo/adapter/pricing_table.json from a fresh copy of
LiteLLM's public pricing data. See pricing.py's module docstring for what
this file is, and pricing_fetch.py for the actual fetch/curation logic
this script just calls and writes to disk.

This table WILL go stale as new models ship. There is no CI job or
schedule that re-runs this automatically. It's a maintainer action, run
by hand periodically or whenever a model real usage depends on is missing
from a `redundo adapt --summary` run's cost coverage. Review the diff
(git diff src/redundo/adapter/pricing_table.json) and commit it like any
other change.

    uv run python scripts/update_pricing_table.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redundo.adapter import pricing_fetch  # noqa: E402

OUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "redundo" / "adapter" / "pricing_table.json"
)


def main() -> int:
    print(f"Fetching {pricing_fetch.SOURCE_URL} ...")
    raw = pricing_fetch.fetch_litellm_pricing_data()
    table = pricing_fetch.build_pricing_table_file(raw)

    OUT_PATH.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    by_provider: dict[str, int] = {}
    for entry in table["entries"].values():
        by_provider[entry["provider"]] = by_provider.get(entry["provider"], 0) + 1

    print(f"Wrote {len(table['entries'])} entries to {OUT_PATH} (generated_at: {table['generated_at']})")
    print("By provider:", by_provider)
    print(f"Review the diff (git diff {OUT_PATH}) before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
