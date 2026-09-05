# Adapter spec: OTLP/OpenInference -> the redundo.analyzer Event schema

This is the contract this adapter implements. It's written down separately
from the code so another implementation (a different language, a
streaming pipeline, whatever) can produce output that's actually
comparable to this adapter's output -- comparability requires identical
behavior, not just "close enough."

## task_id

Prefer `gen_ai.conversation.id` when present. Fall back to the OTLP trace
ID. **Never synthesize a grouping key beyond that.**

Precisely: for each trace, collect the distinct `gen_ai.conversation.id`
values across all of that trace's spans (OTel context propagation means
it's common for only some spans -- often just the root -- to carry it).

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
