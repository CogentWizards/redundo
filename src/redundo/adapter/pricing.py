"""A bundled, static snapshot of per-model token pricing, used to estimate
cost_usd for a record that has real per-call token counts but no cost
figure of its own (OpenInference's gen_ai.usage.* attributes, for
example). This module has no network access of any kind; fetching fresh
pricing data lives in pricing_fetch.py, imported only by the
`redundo update-pricing` CLI and the maintainer-only
scripts/update_pricing_table.py dev script, never by adapt/analyze's own
code path.

pricing_table.json (bundled inside this package) is a curated subset of
LiteLLM's own public model_prices_and_context_window.json
(https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json),
MIT licensed, restricted to direct-API entries for a handful of major
providers. See pricing_fetch.py's WANTED_PROVIDERS/RESELLER_PREFIXES for
exactly which. It WILL go stale as new models ship or providers change a
rate. An unrecognized model simply gets no cost estimate, never a guessed
one; see estimate_cost_usd below.

Staleness of a *recognized* model's price is the worse failure mode: unlike
a missing model (which visibly produces no estimate), a stale price
produces a confident, plausible-looking number indistinguishable from a
correct one. Every pricing file this module reads therefore carries its
own generated_at timestamp, and pricing_staleness_note surfaces its age
unconditionally, not just once it's old.

Two ways to refresh, for two audiences, mirroring the same design already
shipped in the openclaw-localtrace plugin:
- A repo checkout can regenerate the bundled snapshot with
  `python scripts/update_pricing_table.py`, review the diff, and ship it
  in the next release.
- Anyone with only the published package installed can run
  `redundo update-pricing` to fetch the same data and write it to a local
  override file (default_override_path() below, or --out) that this
  module layers OVER the bundled table at runtime (override wins per
  model; bundled still covers everything the override doesn't). No new
  release needed, and no network access except from that one, explicitly
  run command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BUNDLED_PATH = Path(__file__).parent / "pricing_table.json"

# A recognized-but-stale price is worse than a missing one (see module
# docstring). This is when pricing_staleness_note starts saying so
# explicitly instead of just reporting the age.
STALE_AFTER_DAYS = 30


def default_override_path() -> Path:
    return Path.home() / ".redundo" / "pricing-table.json"


@dataclass(frozen=True)
class PricingEntry:
    provider: str
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass
class PricingContext:
    entries: dict[str, PricingEntry] = field(default_factory=dict)
    generated_at: str | None = None


def _load_table_file(path: Path) -> tuple[dict[str, PricingEntry], str | None] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries: dict[str, PricingEntry] = {}
    for model, e in raw.get("entries", {}).items():
        if not isinstance(e, dict) or "input" not in e or "output" not in e:
            continue
        entries[model] = PricingEntry(
            provider=e.get("provider", ""),
            input=float(e["input"]),
            output=float(e["output"]),
            cache_read=float(e["cache_read"]) if e.get("cache_read") is not None else None,
            cache_write=float(e["cache_write"]) if e.get("cache_write") is not None else None,
        )
    return entries, raw.get("generated_at")


def load_pricing_context(override_path: Path | None = None) -> PricingContext:
    """Bundled table, overlaid by a local override file if one exists at
    override_path (default_override_path() when not given). The override
    wins per model; the bundled table still covers everything the
    override doesn't. generated_at reflects whichever of the two is
    actually newer, so a partial override doesn't misreport the whole
    table's age as fresher than the bundled entries it didn't touch.
    """
    bundled = _load_table_file(_BUNDLED_PATH) or ({}, None)
    entries = dict(bundled[0])
    generated_at = bundled[1]

    resolved_override_path = override_path if override_path is not None else default_override_path()
    if resolved_override_path.exists():
        override = _load_table_file(resolved_override_path)
        if override is not None:
            entries.update(override[0])
            if override[1] and (generated_at is None or override[1] > generated_at):
                generated_at = override[1]

    return PricingContext(entries=entries, generated_at=generated_at)


def estimate_cost_usd(
    entries: dict[str, PricingEntry],
    model: str | None,
    *,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    """None whenever the model isn't in the table or there's nothing to
    price. Never a guessed number for an unrecognized model. Matched by
    exact model string only, the same string LiteLLM's own pricing keys
    use for a direct-API call; a provider-prefixed variant not in the
    table (e.g. "openrouter/anthropic/claude-...") degrades to no
    estimate rather than a fuzzy match that could silently price the
    wrong model.
    """
    if not model:
        return None
    entry = entries.get(model)
    if entry is None:
        return None
    if tokens_in is None and tokens_out is None:
        return None
    total = (tokens_in or 0) * entry.input + (tokens_out or 0) * entry.output
    if cache_read_tokens and entry.cache_read is not None:
        total += cache_read_tokens * entry.cache_read
    if cache_write_tokens and entry.cache_write is not None:
        total += cache_write_tokens * entry.cache_write
    return total


def pricing_age_days(generated_at: str | None, *, now: datetime | None = None) -> int | None:
    if not generated_at:
        return None
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - generated).days


def pricing_staleness_note(generated_at: str | None, *, now: datetime | None = None) -> str | None:
    """None when there's no pricing data loaded at all (nothing to report).
    Otherwise always reports the active table's age, escalating past
    STALE_AFTER_DAYS. Age is shown unconditionally, not only once it's
    a problem, so a reader never has to wonder whether silence means
    fresh or means untracked.
    """
    if not generated_at:
        return None
    age = pricing_age_days(generated_at, now=now)
    if age is None:
        return None
    note = f"pricing table generated {generated_at} ({age} day(s) old)"
    if age > STALE_AFTER_DAYS:
        note += (
            f"; over {STALE_AFTER_DAYS} days old, run `redundo update-pricing` "
            "to refresh it"
        )
    return note
