"""Fetches and curates a fresh pricing snapshot from LiteLLM's public
model_prices_and_context_window.json. The only network-capable code in
the pricing system; imported only by the `redundo update-pricing` CLI
(update_pricing_cli.py) and the maintainer-only dev script
(scripts/update_pricing_table.py), never by pricing.py itself or by
adapt/analyze's own code path. See pricing.py's module docstring for why
that separation matters.

Uses only the standard library (urllib), not requests. adapt/analyze
have zero dependencies today, and this module living in the same package
must not change that for anyone who never runs update-pricing.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Restricted to direct-API providers redundo's sources actually report
# models for. Extend this set (and re-run update_pricing_table.py) to
# cover more; this was never meant to mirror LiteLLM's full ~3900-entry
# file, just enough for real usage.
WANTED_PROVIDERS = frozenset({
    "anthropic",
    "openai",
    "gemini",
    "vertex_ai-language-models",
    "xai",
    "deepseek",
    "mistral",
})

# Keys under these prefixes are the same underlying model re-sold through
# a hosting/integration layer, excluded so the table only ever contains
# genuine direct-API pricing, matching the plain model strings a source's
# own gen_ai.request.model attribute actually reports.
RESELLER_PREFIXES = (
    "azure/", "azure_ai/", "bedrock/", "bedrock_converse/", "vertex_ai/",
    "vertex_ai_beta/", "databricks/", "anyscale/", "together_ai/",
    "fireworks_ai/", "openrouter/", "groq/", "perplexity/", "cerebras/",
    "sagemaker/", "sagemaker_chat/", "watsonx/", "replicate/",
    "cloudflare/", "friendliai/", "nvidia_nim/", "deepinfra/", "nscale/",
    "novita/",
)


def fetch_litellm_pricing_data(*, timeout: float = 30.0) -> dict[str, Any]:
    with urllib.request.urlopen(SOURCE_URL, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def curate_pricing_data(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """raw: LiteLLM's own model_prices_and_context_window.json, parsed.
    Returns the curated {model: {provider, input, output, cache_read,
    cache_write}} mapping this package's pricing_table.json stores under
    "entries". Skips any entry missing either input or output cost;
    without both, there's nothing usable to estimate from.
    """
    curated: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("litellm_provider")
        if provider not in WANTED_PROVIDERS:
            continue
        if any(key.startswith(prefix) for prefix in RESELLER_PREFIXES):
            continue
        input_cost = entry.get("input_cost_per_token")
        output_cost = entry.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue
        curated[key] = {
            "provider": provider,
            "input": input_cost,
            "output": output_cost,
            "cache_read": entry.get("cache_read_input_token_cost"),
            "cache_write": entry.get("cache_creation_input_token_cost"),
        }
    return dict(sorted(curated.items()))


def build_pricing_table_file(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The full {"generated_at": ..., "entries": {...}} shape this
    package's pricing_table.json and a user's own override file both use.
    generated_at is load-bearing, not decorative. See pricing.py's
    module docstring on why a stale-but-present price is a worse failure
    than a missing one, and how this timestamp is what makes that
    staleness visible instead of silent.
    """
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "entries": curate_pricing_data(raw),
    }
