# Adapter spec: Claude Cowork native OTLP telemetry -> the redundo.analyzer Event schema

Companion to [SPEC.md](SPEC.md) (Claude Code). Read that one first for the
schema conventions this shares (`content_basis`, the opaque-hash safety
distinction between calls and results, "no price table," `metadata.write`
never set). This document only covers what's genuinely different about
Cowork, based on the official Cowork monitoring reference the user
supplied directly -- not independently captured against a live Cowork
session the way the Claude Code adapter was, since Cowork's OTel export
requires Team/Enterprise admin access this session doesn't have. Treat the
mappings below as spec-verified, not empirically verified -- if real
captured Cowork telemetry ever contradicts something here, trust the
capture, the same way Claude Code's own docs turned out to be wrong about
`tool_use_id`.

## The one structural fact everything else follows from

**Cowork has no span tree, in the common case.** It "exports events via
the OTel logs/events protocol" exclusively. `trace_id`/`span_id`
correlation is a third-party-deployment-only beta feature
(`otlpTracesEnabled`) whose span shape isn't documented anywhere accessible
-- this adapter doesn't attempt to use it, and falls back to the universal
logs-only path regardless of whether those beta fields happen to be
present. `cowork_convert.py` is a separate module from `convert.py` (not a
variant of it) because there is no parent/child structure to walk at all,
not because the walking logic needed tweaking.

## Correlation model

Three attributes replace what a span tree would otherwise provide:

- `session.id` -- task grouping, same role as Claude Code.
- `prompt.id` -- links every event produced while processing one user
  prompt. This is Cowork's equivalent of Claude Code's
  `claude_code.interaction` span, but it's a value shared across several
  *independent* log records, not an actual span any event nests under.
- `event.sequence` -- a monotonic per-session counter across every event
  type. This is the *only* ordering signal: there's no start/end
  timestamp pair per event the way spans have, so there's nothing to
  detect concurrency from and nothing to chain against.

Given that, `parent_id` is left `None` on every record this adapter
emits. `redundo.analyzer.lineage`'s own documented fallback -- when a source
sets no `parent_id`, each event's effective parent is simply the
immediately preceding event in the same task -- **is exactly the right
model for what this source actually provides**, not a degradation from
something better. There's no adapter-side lineage logic to get wrong here,
which is itself worth stating plainly: less can go wrong, but also less is
knowable (see "What's structurally lost" below).

## Event -> schema mapping

| Cowork event | Schema record | Notes |
|---|---|---|
| `api_request` | `llm_call`, outcome `ok` | `model`, `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` all directly on this one event -- no cross-event join needed for cost, unlike Claude Code. |
| `api_error` | `llm_call`, outcome `error` | Kept, not dropped -- a failed request is still something that happened. No cost/token fields (per spec, not provided on this event). |
| `tool_result` | `tool_call` only | See "No tool_result, ever" below. |
| `tool_decision` (`decision="reject"`) | `tool_call`, outcome `error` | An attempt blocked before execution, so it has no corresponding `tool_result` to conflict with. |
| `tool_decision` (`decision="accept"`) | *(nothing)* | The same call's `tool_result` covers it; converting both would double-count every successful call. |
| `user_prompt` | *(nothing -- content source only)* | Feeds the first `api_request`/`api_error` per `prompt.id`, same "first completion of a turn" rule as Claude Code. |
| `assistant_response` | *(nothing -- content source only)* | Joined to its `api_request`/`api_error` by the shared `request_id`; populates `metadata.response_hash`. |

## `content_hash` sources

- **`llm_call`** (from `api_request`/`api_error`): the interaction-level
  `user_prompt.prompt` text, but only for the *first* such event per
  `prompt.id` -- every `api_request`/`api_error` sharing a `prompt.id`
  shares the same correlation value, so "is this event's prompt.id
  present in the prompt map" can't distinguish first-in-turn from a later
  round the way it could if there were real nesting; the adapter tracks
  which `prompt.id`s have already been claimed instead, same mechanism as
  `first_llm_request_of_interaction` in `convert.py`. Later completions in
  the same prompt's tool loop get an opaque hash (see SPEC.md's "Opaque
  content" section for why that's safe here, exactly as it is for Claude
  Code).
- **`tool_call`** (from `tool_result` / rejected `tool_decision`): the
  `tool_input` JSON field, present for every tool including MCP ones --
  Cowork's single-event design means there's no positional-join fragility
  here the way Claude Code's split call/result spans needed. MCP calls are
  qualified from `tool_parameters` (`mcp_server_name`/`mcp_tool_name`)
  into the same `mcp__server__tool` naming convention `convert.py` uses,
  for cross-source consistency.

## `metadata.response_hash`: available for Cowork, not for Claude Code

`assistant_response.response`, joined by `request_id`, gives every
`api_request` (not just the first one per turn) a real completion-text
hash when response capture is enabled -- something Claude Code's native
telemetry structurally cannot provide at all, on any signal, under any
flag (see SPEC.md). This means `result_signal` for `llm_call` candidate
pairs can actually reach `WASTE_SUPPORTING`/`LEGIT_SUPPORTING` for Cowork
traces with this enabled, where it's permanently `UNKNOWN` for Claude
Code. Worth knowing when comparing bucket distributions across the two
sources -- a difference in `confirmed_waste` reachability between them is
not a bug, it's a real difference in what each source can observe.

## No `tool_result`, ever

`tool_result` (the event) never carries actual output content under any
documented configuration -- only `tool_result_size_bytes`, a byte count.
This is architecturally absent, not gated behind a flag the way Claude
Code's built-in-tool output is: there is no `otlpContentCapture` option
that adds result content, and the doc enumerates every content-gated field
explicitly (`userPrompts`, `assistantResponses`, `toolDetails`) without a
fourth for tool output.

Given that, this adapter never emits a `tool_result` schema record from
Cowork telemetry at all. Fabricating one with a placeholder content_hash
would be actively unsafe for the identical reason documented at length in
`convert.py`: `classify.py`'s `_result_signal()` treats a content_hash
*mismatch* as positive evidence the result changed, so a per-record opaque
hash would manufacture a false `likely_legitimate` verdict on every tool
candidate pair, and a shared sentinel hash would manufacture the opposite
false `confirmed_waste`-feeding claim. The only safe representation of
"unknown result" this schema has is no `tool_result` event at all.

**Consequence, stated plainly**: `result_signal` reads `UNKNOWN` for every
tool `CandidatePair` this adapter ever produces, always -- not sometimes,
not depending on a flag. `confirmed_waste` for a Cowork tool-call repeat is
therefore unreachable through this signal alone; it would need `write`
status confirmed absent *and* task-level failure, with the result gap
absorbed by the demotion rule the same way a stripped-result Claude Code
trace degrades to `unclassified` rather than a false positive (see the
main project's `--strip-result-hash` validation work) -- this is the same
honest degradation, not a special case.

**To partially recover outcome tracking despite this**, `outcome` is set
directly on the `tool_call` record (from `tool_result.success` or the
rejected-`tool_decision` case) rather than left `None` the way
`redundo.adapter.sources.openinference` and `convert.py` do for call-type events. This is
a deliberate departure, safe because `_terminal_signal()` reads whichever
event is last by `step_index` regardless of `event_type`, and no other
signal (`_write_signal`, `_correlated_result_hash`) reads a `tool_call`'s
own `outcome` field for anything -- it only affects task-level
terminal-outcome tracking in the case where the last event in a task
happens to be a tool call with no result. Without this, tasks whose last
action was a tool call would lose terminal-outcome visibility entirely, on
top of already losing result-identity comparison.

## What's structurally lost versus Claude Code

- **No genuine concurrency detection.** Claude Code's real span timestamps
  let `convert.py` at least attempt to distinguish overlapping tool calls
  from sequential ones (even though that heuristic turned out to be wrong
  for llm_request/tool overlap specifically -- see SPEC.md). Cowork's
  `event.sequence` is a pure ordering signal with no duration information
  at all, so there's nothing to even attempt that distinction with. Every
  event chains linearly regardless of whether the underlying execution was
  actually sequential.
- **No subagent/workflow segmentation.** No `agent_id`/`parent_agent_id`
  equivalent is documented for Cowork events. `workflow` is always `None`.
- **No `tool_use_id`-equivalent at all**, on either the call or result
  side of a `tool_result` event -- not needed, since call and result are
  the same record, but also not available if a future need arose to
  correlate a `tool_decision` to its specific later `tool_result`
  (currently only attempted for the reject case, which needs no
  correlation since a rejected call structurally can't have a later
  result).

## Setup

Configured through the Cowork admin UI (Admin settings > Cowork), not
environment variables -- point the OTLP endpoint at the same collector
`claude_code_otlp_collector.py` already runs (it already listens on
`/v1/logs`, which is all Cowork ever uses; no separate collector needed).
Enable `otlpContentCapture` (`userPrompts`, `assistantResponses`,
`toolDetails`) for the content this adapter actually uses -- without it,
every record degrades to opaque content, same tradeoff as Claude Code's
`OTEL_LOG_*` flags.

Point the collector at `/v1/logs` -- the same `redundo collect` used
for the other sources also serves that endpoint, no separate collector
needed. The source is auto-detected from the data; `redundo adapt`
works unchanged here too:

```bash
redundo adapt ./otlp_traces -o cowork_trace.jsonl --summary
redundo analyze cowork_trace.jsonl --format html --output report.html
```
