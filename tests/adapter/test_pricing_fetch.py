"""Tests for pricing_fetch.py's curation logic. No real network call
anywhere here: curate_pricing_data/build_pricing_table_file operate on an
in-memory dict shaped like LiteLLM's real model_prices_and_context_window.json.
"""

from datetime import datetime, timezone

from redundo.adapter.pricing_fetch import build_pricing_table_file, curate_pricing_data


def test_keeps_wanted_provider_entries_with_both_costs():
    raw = {
        "claude-sonnet-4-5-20250929": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 1.5e-5,
            "cache_read_input_token_cost": 3e-7,
            "cache_creation_input_token_cost": 3.75e-6,
        },
    }
    curated = curate_pricing_data(raw)
    assert curated["claude-sonnet-4-5-20250929"] == {
        "provider": "anthropic",
        "input": 3e-6,
        "output": 1.5e-5,
        "cache_read": 3e-7,
        "cache_write": 3.75e-6,
    }


def test_drops_entries_from_an_unwanted_provider():
    raw = {
        "some-model": {
            "litellm_provider": "cohere",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    }
    assert curate_pricing_data(raw) == {}


def test_drops_reseller_hosted_key_prefixes():
    raw = {
        "azure/gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
        "openrouter/anthropic/claude-3": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    }
    assert curate_pricing_data(raw) == {}


def test_drops_entries_missing_either_cost():
    raw = {
        "no-output-cost": {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-6,
        },
        "no-input-cost": {
            "litellm_provider": "openai",
            "output_cost_per_token": 1e-6,
        },
    }
    assert curate_pricing_data(raw) == {}


def test_missing_cache_costs_become_none_not_zero():
    raw = {
        "gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    }
    curated = curate_pricing_data(raw)
    assert curated["gpt-4o"]["cache_read"] is None
    assert curated["gpt-4o"]["cache_write"] is None


def test_build_pricing_table_file_shape_and_generated_at():
    raw = {
        "gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    }
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    table = build_pricing_table_file(raw, now=now)
    assert table["generated_at"] == "2026-01-01T00:00:00Z"
    assert "gpt-4o" in table["entries"]
