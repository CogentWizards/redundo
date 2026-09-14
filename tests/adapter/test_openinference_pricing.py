"""cost_usd estimation wired into the openinference adapter. See
pricing.py for the estimator itself (tested in isolation in
test_pricing.py). These tests exercise convert_openinference's own
plumbing: when it estimates, when it doesn't, and what it reports.
"""

from redundo.adapter.pricing import PricingContext, PricingEntry
from redundo.adapter.sources.openinference import convert_openinference
from helpers import span, traces_document


def _ctx(**entries):
    return PricingContext(entries=entries, generated_at="2026-01-01T00:00:00Z")


def test_estimates_cost_from_tokens_when_no_direct_cost_attr():
    ctx = _ctx(**{"gpt-4o": PricingEntry(provider="openai", input=1e-6, output=2e-6)})
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
    })]
    records, summary = convert_openinference([traces_document(spans)], pricing_context=ctx)
    record = records[0]
    assert record["cost_usd"] == 1000 * 1e-6 + 500 * 2e-6
    assert record["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"
    assert summary.pricing_table_generated_at == "2026-01-01T00:00:00Z"


def test_direct_cost_attr_wins_over_estimation():
    ctx = _ctx(**{"gpt-4o": PricingEntry(provider="openai", input=1e-6, output=2e-6)})
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
        "llm.cost.total": 0.123,
    })]
    records, summary = convert_openinference([traces_document(spans)], pricing_context=ctx)
    assert records[0]["cost_usd"] == 0.123
    assert "cost_basis" not in records[0]["metadata"]
    assert summary.pricing_table_generated_at is None


def test_unrecognized_model_stays_none_never_a_guess():
    ctx = _ctx(**{"gpt-4o": PricingEntry(provider="openai", input=1e-6, output=2e-6)})
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "some-model-not-in-the-table",
        "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
    })]
    records, summary = convert_openinference([traces_document(spans)], pricing_context=ctx)
    assert records[0]["cost_usd"] is None
    assert "cost_basis" not in records[0]["metadata"]
    assert summary.pricing_table_generated_at is None


def test_cache_tokens_included_in_the_estimate():
    ctx = _ctx(**{
        "claude-x": PricingEntry(
            provider="anthropic", input=3e-6, output=1.5e-5,
            cache_read=3e-7, cache_write=3.75e-6,
        ),
    })
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "claude-x",
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "gen_ai.usage.cache_creation.input_tokens": 200,
    })]
    records, _ = convert_openinference([traces_document(spans)], pricing_context=ctx)
    expected = 100 * 3e-6 + 50 * 1.5e-5 + 1000 * 3e-7 + 200 * 3.75e-6
    assert records[0]["cost_usd"] == expected


def test_summary_notes_report_pricing_table_staleness():
    stale_ctx = PricingContext(
        entries={"gpt-4o": PricingEntry(provider="openai", input=1e-6, output=2e-6)},
        generated_at="2000-01-01T00:00:00Z",
    )
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50,
    })]
    _, summary = convert_openinference([traces_document(spans)], pricing_context=stale_ctx)
    assert any("cost_usd was estimated" in n and "update-pricing" in n for n in summary.notes())


def test_no_estimation_attempted_when_no_pricing_note_in_summary():
    spans = [span("s1", start=0, attributes={
        "openinference.span.kind": "LLM", "input.value": "hi",
        "gen_ai.request.model": "gpt-4o",
    })]
    _, summary = convert_openinference(
        [traces_document(spans)], pricing_context=PricingContext(entries={}, generated_at=None)
    )
    assert summary.pricing_table_generated_at is None
    assert not any("cost_usd was estimated" in n for n in summary.notes())
