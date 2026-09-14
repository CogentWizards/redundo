"""Tests for pricing.py. No network access anywhere in this file (that's
pricing_fetch.py's job, see test_pricing_fetch.py). Focused on the two
things that matter most: an unrecognized model never produces a guessed
cost, and an override always wins over the bundled table per model.
"""

from datetime import datetime, timezone
from pathlib import Path

from redundo.adapter.pricing import (
    PricingEntry,
    estimate_cost_usd,
    load_pricing_context,
    pricing_age_days,
    pricing_staleness_note,
)


def _entry(**overrides):
    defaults = {"provider": "test", "input": 1e-6, "output": 2e-6}
    defaults.update(overrides)
    return PricingEntry(**defaults)


# --- estimate_cost_usd ----------------------------------------------------

def test_unrecognized_model_never_produces_a_guessed_cost():
    assert estimate_cost_usd({}, "not-a-real-model", tokens_in=100, tokens_out=50) is None


def test_no_model_never_produces_a_guessed_cost():
    entries = {"gpt-4o": _entry()}
    assert estimate_cost_usd(entries, None, tokens_in=100, tokens_out=50) is None


def test_no_token_counts_produces_no_estimate():
    entries = {"gpt-4o": _entry()}
    assert estimate_cost_usd(entries, "gpt-4o", tokens_in=None, tokens_out=None) is None


def test_estimate_multiplies_tokens_by_rate():
    entries = {"gpt-4o": _entry(input=1e-6, output=2e-6)}
    cost = estimate_cost_usd(entries, "gpt-4o", tokens_in=1000, tokens_out=500)
    assert cost == 1000 * 1e-6 + 500 * 2e-6


def test_cache_tokens_included_only_when_the_entry_has_a_cache_rate():
    entries = {"gpt-4o": _entry(cache_read=5e-7, cache_write=None)}
    with_cache_read = estimate_cost_usd(
        entries, "gpt-4o", tokens_in=100, tokens_out=0, cache_read_tokens=1000
    )
    without_cache_read = estimate_cost_usd(entries, "gpt-4o", tokens_in=100, tokens_out=0)
    assert with_cache_read == without_cache_read + 1000 * 5e-7

    # cache_write has no rate on this entry, must not silently price it at 0
    # in a way indistinguishable from "priced and it was free"; it's simply
    # not added at all, same result as omitting cache_write_tokens entirely.
    with_cache_write = estimate_cost_usd(
        entries, "gpt-4o", tokens_in=100, tokens_out=0, cache_write_tokens=1000
    )
    assert with_cache_write == without_cache_read


def test_exact_string_match_only_no_fuzzy_provider_prefix_matching():
    entries = {"claude-sonnet-4-5-20250929": _entry()}
    # A provider-prefixed variant of a real model must not fuzzy-match.
    # See pricing.py's own docstring on why: a wrong match is worse than no
    # estimate.
    assert estimate_cost_usd(
        entries, "anthropic/claude-sonnet-4-5-20250929", tokens_in=100, tokens_out=50
    ) is None


# --- load_pricing_context: override layering ------------------------------

def test_override_wins_per_model_bundled_still_covers_the_rest(tmp_path, monkeypatch):
    import redundo.adapter.pricing as pricing_module

    bundled_path = tmp_path / "bundled.json"
    bundled_path.write_text(
        '{"generated_at": "2020-01-01T00:00:00Z", "entries": '
        '{"model-a": {"provider": "p", "input": 1e-6, "output": 2e-6}, '
        '"model-b": {"provider": "p", "input": 1e-6, "output": 2e-6}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(pricing_module, "_BUNDLED_PATH", bundled_path)

    override_path = tmp_path / "override.json"
    override_path.write_text(
        '{"generated_at": "2026-01-01T00:00:00Z", "entries": '
        '{"model-a": {"provider": "p", "input": 9e-6, "output": 9e-6}}}',
        encoding="utf-8",
    )

    ctx = load_pricing_context(override_path=override_path)
    assert ctx.entries["model-a"].input == 9e-6  # override won
    assert ctx.entries["model-b"].input == 1e-6  # bundled still covers this one
    assert ctx.generated_at == "2026-01-01T00:00:00Z"  # newer of the two


def test_missing_override_file_falls_back_to_bundled_only(tmp_path, monkeypatch):
    import redundo.adapter.pricing as pricing_module

    bundled_path = tmp_path / "bundled.json"
    bundled_path.write_text(
        '{"generated_at": "2020-01-01T00:00:00Z", "entries": '
        '{"model-a": {"provider": "p", "input": 1e-6, "output": 2e-6}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(pricing_module, "_BUNDLED_PATH", bundled_path)

    ctx = load_pricing_context(override_path=tmp_path / "does-not-exist.json")
    assert ctx.entries["model-a"].input == 1e-6
    assert ctx.generated_at == "2020-01-01T00:00:00Z"


# --- staleness --------------------------------------------------------------

def test_pricing_age_days_computed_against_a_fixed_now():
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert pricing_age_days("2026-01-01T00:00:00Z", now=now) == 31


def test_pricing_staleness_note_none_when_no_table_loaded():
    assert pricing_staleness_note(None) is None


def test_pricing_staleness_note_escalates_past_threshold():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fresh = pricing_staleness_note("2026-05-20T00:00:00Z", now=now)
    stale = pricing_staleness_note("2026-01-01T00:00:00Z", now=now)
    assert "update-pricing" not in fresh
    assert "update-pricing" in stale
