# Adapter spec: OTLP/OpenInference -> the redundo.analyzer Event schema

This is the contract this adapter implements. It's written down separately
from the code so another implementation (a different language, a
streaming pipeline, whatever) can produce output that's actually
comparable to this adapter's output -- comparability requires identical
behavior, not just "close enough."

## task_id

Prefer `gen_ai.conversation.id` when present, or `session.id` (the same
concept under OpenInference's own separate attribute name; confirmed
real for Google ADK, whose own OpenInference instrumentation maps ADK's
session id onto `session.id` and never touches `gen_ai.conversation.id`
at all). Fall back to the OTLP trace ID. **Never synthesize a grouping
key beyond that.**

Precisely: for each trace, collect the distinct value each span reports
under either attribute (preferring `gen_ai.conversation.id` if a single
span somehow carried both; not expected in practice, since these are one
source's own choice of attribute name, not two independent signals that
could disagree). OTel context propagation means it's common for only
some spans, often just the root, to carry it at all.

- Exactly one distinct value found -> use it as `task_id` for every span
  in the trace.
- Zero values found -> use the trace ID. Report this: "no
  gen_ai.conversation.id found; grouped by trace -- cross-trace rework not
  detected."
- More than one distinct value found (spans disagree) -> use the trace ID
  and report the ambiguity separately. Don't pick one of the conflicting
  values; that would be a guess dressed up as a decision.

Every emitted record's `metadata.task_id_source` is `"conversation_id"` or
`"trace_id_fallback"`, naming which of the two happened for that specific
record. This is what lets a downstream report state a coverage figure
("N% of records grouped by a real conversation id") instead of a reader
having to trust the grouping blindly -- see redundo analyze's own report
for where this gets surfaced.

Why this matters more than it looks like it should: a fabricated grouping
key produces confidently wrong repeat counts, not a visible gap. Two
genuinely unrelated tasks grouped under a made-up shared ID will show
"repeats" that never happened. A trace-ID fallback that's honestly
reported is a visible, explainable degradation instead -- worse recall,
not wrong data.

## `metadata.parent_task_id`: a real cross-task link, when a source has one

`task_id` groups events within one conversation; it never crosses into a
different task, even when that other task is a subagent this one
delegated to. `metadata.parent_task_id` is a separate, additive signal
for exactly that case: the `task_id` of another task this one was
delegated from, set only when the source itself reports a real
delegation relationship, never inferred from timing, content, or
anything else.

Confirmed real and read today for one source: `hermes-otel` emits
`hermes.subagent.parent_session_id` on every subagent span. No other
source currently exposes an equivalent (checked directly: OpenClaw's own
internal session state tracks a comparable `parentSessionKey`, but it
isn't on any hook payload a plugin can read today; Claude Code's
in-process subagent delegation is already real span nesting under the
parent `Task` call, so it needs no separate link at all).

Nothing in the analyzer consumes this key yet, it's an adapter-level,
schema-documented signal only for now.

## Span kind -> event_type

Only `openinference.span.kind` values of `LLM` and `TOOL` are converted.
Everything else (`CHAIN`, `AGENT`, `RETRIEVER`, `EMBEDDING`, absent) is
skipped and counted, not guessed at -- none of them map cleanly onto
`llm_call` / `tool_call` / `tool_result`.

| OpenInference kind | Produces |
|---|---|
| `LLM` | one `llm_call` record |
| `TOOL` | one `tool_call` record, plus one `tool_result` record if `output.value` is present |
| anything else | nothing; counted in the conversion summary |

A `TOOL` span with no `input.value` is dropped entirely (no arguments, no
candidate for repeat detection). A `TOOL` span with `input.value` but no
`output.value` still produces its `tool_call` record -- the analyzer's
result-identity signal for it will correctly read as unknown.

## Lineage (`parent_id`)

Walks the real OTLP `parentSpanId` chain, transparently skipping
non-kept-kind ancestors to find the nearest one that was actually
converted. A `tool_result` record's `parent_id` is always its own
`tool_call`'s step index. If a kept span has no kept ancestor at all,
`parent_id` is `None` -- the analyzer's own linear-fallback default
applies from there, not a guess made here.

## Content hashing

Shared by every source, not implemented per-source -- see
[docs/hashing.md](hashing.md) for the full procedure (algorithm,
normalization, masking order, versioning, and a copyable ~15-line
reference implementation).

## cost_usd: a direct attribute first, an estimate second

Cost is not a stable OpenInference/gen_ai convention. This adapter first
looks for `llm.cost.total`/`cost.total_usd` directly on the span; when
neither is present, it estimates `cost_usd` itself from real per-call
token counts (`gen_ai.usage.input_tokens`/`output_tokens`, plus
cache-read/cache-write variants when present) against a bundled,
LiteLLM-derived pricing table, the same idea `openclaw-localtrace` already
uses for OpenClaw. See [`pricing.py`](../src/redundo/adapter/pricing.py)'s
own module docstring for exactly what the table is, how it's curated, and
the two ways to refresh it (`scripts/update_pricing_table.py` for a repo
checkout, `redundo update-pricing` for anyone with only the package
installed).

An unrecognized model gets no cost estimate, ever, not a guessed number:
matching is by exact model string only (the same string LiteLLM's own
pricing keys use for a direct-API call), never a fuzzy or
provider-prefix-stripped match. When an estimate is used,
`metadata.cost_basis` is set to `"estimated_from_bundled_pricing_table"`
and `--summary` reports the active pricing table's age unconditionally,
escalating past 30 days old, the same "staleness is shown, not hidden"
discipline as everywhere else in this project.

## Content and token counts split across two spans (hermes-otel)

Found against a real capture, not assumed: at least one real exporter
(`hermes-otel`, feeding Hermes) doesn't put `input.value` and
`gen_ai.usage.*` on the same span. It emits an outer `LLM`-kind span
carrying full content with no token counts, and its own direct child
`LLM`-kind span carrying token counts with no content. Neither span
alone has enough to price correctly.

`_find_llm_token_donor_child` reunites them: when a kept `LLM` span has
content but no token counts of its own, it looks for a direct child span
that is itself `LLM`-kind, has no content, and does carry token counts
(including cache-read/cache-write), and borrows those onto the parent's
record. `ConversionSummary.merged_token_child_spans` counts how often
this happened, surfaced in `--summary`.

This is a real OTLP parent/child relationship here (confirmed via
`parentSpanId` against a live capture), not two siblings that only
overlap in time, so a direct child lookup is enough. That's simpler than
`sources/openclaw_localtrace.py`'s structurally similar split
(`model.call`/`llm.call`), which needs a temporal-overlap heuristic
because those two spans are true siblings that bracket rather than
nest. A source where one span already carries both content and tokens is
untouched by this: the donor lookup only fires when the parent's own
token attributes are absent.
