"""Token-count merging for sources that split a call's content and its
token usage across two spans instead of one. Confirmed against a real
hermes-otel capture (see docs/openinference.md's "cost_usd" section and
CogentWizards/redundo#21): an outer LLM-kind span carries full content
with no gen_ai.usage.* attributes, and its own LLM-kind child span
carries token counts with no content. Neither span alone has enough to
price; _find_llm_token_donor_child reunites them via the real OTLP
parent/child relationship (no temporal-overlap heuristic needed, unlike
sources/openclaw_localtrace.py's structurally similar split).
"""

from redundo.adapter.pricing import PricingContext, PricingEntry
from redundo.adapter.sources.openinference import convert_openinference
from helpers import span, traces_document


def test_child_token_counts_merge_into_the_parents_record():
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
            "gen_ai.request.model": "gpt-4o",
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
        }),
    ]
    records, summary = convert_openinference([traces_document(spans)])
    llm_records = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_records) == 1
    assert llm_records[0]["tokens_in"] == 1000
    assert llm_records[0]["tokens_out"] == 500
    assert summary.merged_token_child_spans == 1
    assert any("merged token counts" in n for n in summary.notes())


def test_cache_tokens_merge_too_and_feed_cost_estimation():
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
            "gen_ai.request.model": "claude-x",
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50,
            "gen_ai.usage.cache_read.input_tokens": 1000,
            "gen_ai.usage.cache_creation.input_tokens": 200,
        }),
    ]
    ctx = PricingContext(
        entries={
            "claude-x": PricingEntry(
                provider="anthropic", input=3e-6, output=1.5e-5,
                cache_read=3e-7, cache_write=3.75e-6,
            ),
        },
        generated_at="2026-01-01T00:00:00Z",
    )
    records, _ = convert_openinference([traces_document(spans)], pricing_context=ctx)
    record = [r for r in records if r["event_type"] == "llm_call"][0]
    expected = 100 * 3e-6 + 50 * 1.5e-5 + 1000 * 3e-7 + 200 * 3.75e-6
    assert record["cost_usd"] == expected
    assert record["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"


def test_no_merge_when_the_parent_already_has_its_own_tokens():
    # Real case for most OpenInference sources: one span carries both.
    # A same-shaped child must never overwrite already-present data.
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5,
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.usage.input_tokens": 9999, "gen_ai.usage.output_tokens": 9999,
        }),
    ]
    records, summary = convert_openinference([traces_document(spans)])
    llm_records = [r for r in records if r["event_type"] == "llm_call"]
    parent_record = [r for r in llm_records if r["tokens_in"] == 10][0]
    assert parent_record["tokens_out"] == 5
    assert summary.merged_token_child_spans == 0


def test_child_with_its_own_content_is_a_real_second_record_not_a_donor():
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "LLM", "input.value": "a real second call",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
        }),
    ]
    records, summary = convert_openinference([traces_document(spans)])
    llm_records = [r for r in records if r["event_type"] == "llm_call"]
    assert len(llm_records) == 2
    assert summary.merged_token_child_spans == 0
    # The parent's own record must still show no tokens: nothing to
    # borrow from a child that has its own content, that child isn't a
    # donor, it's an independent call.
    no_token_records = [r for r in llm_records if r["tokens_in"] is None]
    assert len(no_token_records) == 1


def test_tool_kind_child_is_never_treated_as_a_token_donor():
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "TOOL", "tool.name": "search",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
        }),
    ]
    records, summary = convert_openinference([traces_document(spans)])
    llm_record = [r for r in records if r["event_type"] == "llm_call"][0]
    assert llm_record["tokens_in"] is None
    assert summary.merged_token_child_spans == 0


def test_model_borrowed_from_donor_only_when_parent_has_none():
    spans = [
        span("parent", start=0, attributes={
            "openinference.span.kind": "LLM", "input.value": "hi",
        }),
        span("child", parent_span_id="parent", start=1, attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500,
        }),
    ]
    records, _ = convert_openinference([traces_document(spans)])
    llm_record = [r for r in records if r["event_type"] == "llm_call"][0]
    assert llm_record["model"] == "gpt-4o"
