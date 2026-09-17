# Pricing data

When a source reports `cost_usd` directly, that value is used as-is,
never recomputed. When it doesn't, but the record has real per-call
token counts, `redundo` estimates `cost_usd` from a bundled pricing
snapshot instead.

`pricing_table.json` (bundled inside the package) is a curated subset of
[LiteLLM's public pricing data](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json),
covering direct-API entries for a handful of major providers. It is
never fetched over the network at runtime. It's a plain
provider-published-rate estimate: it doesn't know about your own
negotiated or discounted pricing, and it goes stale as new models ship.
An unrecognized model simply gets no cost estimate, never a guessed one.

Matching is by exact model string only, never a fuzzy or
provider-prefix-stripped match. When an estimate is used,
`metadata.cost_basis` is set to `"estimated_from_bundled_pricing_table"`,
and every report's coverage line states the active table's age
unconditionally, not only once it's stale, escalating past 30 days old.

## Refreshing the snapshot

Two ways to refresh, for two audiences:

**Anyone with the published package installed:**

```bash
redundo update-pricing
```

Fetches a fresh copy of LiteLLM's data and writes it to a local override
file (`~/.redundo/pricing-table.json` by default, or `--out PATH`). This
is the only command in `redundo`'s normal operation that makes an
outbound network call, and only when run by hand. An override entry wins
per model; anything it doesn't cover still falls back to the bundled
snapshot, so a small or slightly stale override never regresses coverage
for everything else.

**A repo checkout, ahead of the next release:**

```bash
python scripts/update_pricing_table.py
```

Regenerates the bundled snapshot itself. Review the diff, and commit it.

Neither path touches the network unless you run one of these two
commands yourself.
