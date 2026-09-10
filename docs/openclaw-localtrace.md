# Adapter spec: `openclaw-localtrace` -> the redundo.analyzer Event schema

[`openclaw-localtrace`](https://github.com/CogentWizards/openclaw-localtrace)
is a separate, purpose-built OpenClaw plugin -- not a config knob on
`@openclaw/diagnostics-otel` (see docs/openclaw.md). It exists because that
official exporter's own privacy design (deliberate, documented, and
correct for its purpose) strips every session/run identifier and reports
no write/mutation signal at all, which is exactly the data this package's
redundancy analysis needs most. Rather than argue with that tradeoff, the
plugin removes the one thing that made it necessary: it writes only to
the local filesystem, never a network endpoint.

This document is the Python-side half of that story -- what the plugin
actually emits, and how this adapter turns it into `redundo.analyzer`'s
Event schema. Every claim below is checked against the plugin's own
TypeScript source (`src/spans.ts`, `src/metrics.ts`) and a real capture
from a live Gateway, including two real bugs found only by that live
capture (see "Two real bugs, found only by running it" below) -- not
against its README alone.

## Span kinds

| Span name | Becomes |
|---|---|
| `openclaw-localtrace.model.call` | `llm_call` (ids/timing/outcome only -- see "Two spans per model call" below) |
| `openclaw-localtrace.tool.execution` | `tool_call` (+ `tool_result` when output was captured) |
| `openclaw-localtrace.llm.call` | nothing on its own -- merged into the `llm_call` record produced by its sibling `model.call` span |
| `openclaw-localtrace.run` | nothing -- structural wrapper span (one per agent turn), used only for the `workflow` label and the run-window boundaries cost apportionment needs |

Detection: a span name starting with `openclaw-localtrace.`, or a resource
`service.name` of `openclaw-localtrace`. The hyphen is load-bearing --
`"openclaw-localtrace.".startswith("openclaw.")` is `False`, so this can
never collide with `sources.openclaw`'s own detection, by construction, not
by luck.

## Two spans per model call, and why they have to be merged, not enriched

The plugin emits `openclaw-localtrace.model.call` (ids, timing, provider/
model, outcome -- **no permission opt-in needed at all**, since it carries
no content) and `openclaw-localtrace.llm.call` (prompt/response content and
token usage -- gated behind the plugin's own `captureContent` config *and*
the OpenClaw host's `hooks.allowConversationAccess` permission) as two
**sibling** spans, not one span with the second enriching the first.

That split exists because of a real ordering bug found and fixed in the
plugin itself: `llm_input` fires *before* `model_call_started`, and
`llm_output` fires *after* `model_call_ended` -- the two spans bracket each
other rather than nesting, and OTel spans reject attribute writes after
`.end()`, so the plugin has no way to write `llm.call`'s content onto an
already-closed `model.call` span.

**`llm.call` is scoped to the whole run (turn), not to one individual
`model.call`.** Confirmed against a real capture with a 13-iteration tool
loop inside one run that produced exactly ONE `llm.call` span, whose own
`[start, end)` interval contains all 13 `model.call` spans (opens before
the first `model_call_started`, closes after the last
`model_call_ended`). An earlier version of this adapter assumed a 1:1
pairing -- matching an early, smaller-scale test -- and required matching
counts before pairing anything at all, which meant a real run with N
`model.call` spans and 1 `llm.call` span got **no pairing whatsoever**,
silently discarding content and token data (and, by extension, the cost
estimate below) for every one of that run's `llm_call` events. This was a
real, confirmed production bug, not a hypothetical: a user's own capture
showed cost coverage of roughly 2% of tracked spend because of it, on top
of the separate `reply_payload_sending` gap described below.

Fixed: each `llm.call` span is paired with the **last** `model.call` span
it temporally contains (`_pair_llm_call_spans`) -- the one whose response
most plausibly produced the captured `assistantTexts`, since `llm_output`
describes that final attempt, not each intermediate tool-loop step. Every
earlier `model.call` in the same run keeps its ids/timing but degrades
content_hash to `content_basis: "opaque"`, the same idiom every other
source in this package uses for its own no-content case -- not a gap, an
accurate reflection of what this signal was ever designed to expose. An
`llm.call` span containing no `model.call` span at all (a genuinely empty
bracket) is left unpaired rather than guessed at --
`summary.llm_call_spans_unpaired` reports how often this happened.

## task_id: the real session id, for the first time from an OpenClaw source

Every other OpenClaw-derived source in this package (`sources.openclaw`,
built against `@openclaw/diagnostics-otel`) is permanently trace-scoped --
that exporter actively strips every session identifier by design, not as
an occasional gap. `openclaw-localtrace` was built specifically to keep
what that design discards: when the plugin's own `captureIdentifiers`
config is on (off by default), every span carries a real `openclaw.sessionId`
attribute -- the actual OpenClaw conversation id, stable across every turn
in that conversation, not just within one trace.

This adapter uses it as `task_id` directly (`metadata.task_id_source =
"conversation_id"`), and groups records from *different* trace ids sharing
the same real session id into one task with continuous `step_index`
ordering -- redundancy spanning more than one turn is detectable from an
OpenClaw source for the first time. Without `captureIdentifiers`, there is
no session attribute on any span at all, and every record falls back to
its own trace id, same "ceiling, not fallback" framing docs/openclaw.md
uses for its own ever-present trace_id.

## The write signal: the single most valuable capability unlock

`@openclaw/diagnostics-otel` reports no write/mutation signal at all --
`sources.openclaw` always leaves `classify.py`'s write signal at `UNKNOWN`.
`openclaw-localtrace`'s `tool.execution` spans carry
`openclaw.mutatingAction`, a boolean the plugin computes from its own
`mutatingToolNames` config (default: `exec`, `apply_patch`, `write_file`,
`edit_file`, `delete_file`) -- **not** a host-computed signal the way the
old diagnostics-bus design would have had, since the hook surface this
plugin is built on has no such field itself, but a real, inspectable,
operator-tunable classification nonetheless. This adapter copies it
straight onto the `tool_call` record's `metadata.write`, never onto the
paired `tool_result` (matching `classify.py`'s own reasoning: a result row
is an outcome, not an action, and isn't asked for a write flag).

## cost_usd: two independent tiers, a direct estimate first

Two entirely separate signals feed `cost_usd`, and this adapter prefers
whichever is more precise on a per-record basis.

**Tier 1: a direct per-call estimate.** The plugin itself now computes
`gen_ai.usage.cost_usd` on `llm.call` spans, from that call's own token
usage against a bundled static pricing snapshot (see the plugin's own
README) -- not from OpenClaw at all. This adapter copies it straight onto
the record (`metadata.cost_basis = "estimated_from_bundled_pricing_table"`)
whenever present. It is a plain provider-published-rate estimate, not a
certified per-call billed amount, and it only reaches whichever
`llm_call` records the pairing above actually reached.

**Tier 2: `openclaw.turn.cost.usd`, apportioned.** `sources.openclaw`'s
own cost signal (`openclaw.cost.usd`) is a cumulative Counter that resets
every time the exporting Gateway process restarts, needing a real
"generation" concept to apportion correctly (see docs/openclaw.md).
`openclaw-localtrace`'s `openclaw.turn.cost.usd` is a **Gauge** with one
independent point per agent turn -- no restart-generation ambiguity to
resolve, because there's no running total to begin with. Each point is
apportioned across the one *run*'s own `llm_call` records that **don't
already have a tier-1 estimate** -- by token share when they have token
data, evenly otherwise -- matched to the run, for the same session,
whose own `[start, end)` window ends most recently at or before the
point's own timestamp.

**This second tier turned out to have real coverage gaps in practice,
confirmed against a real capture**: `openclaw.turn.cost.usd` only fires
on certain live-dispatcher-delivered replies (OpenClaw's own hook docs:
absent on "durable delivery, recovered replay, and replies without exact
run correlation") -- a user's own session had two real agent runs, and
only one of them ever produced a usage snapshot at all, leaving the
other's real spend completely untracked by this tier alone. Tier 1 does
not depend on reply delivery at all, so it has meaningfully better
coverage in practice, bounded only by the `llm.call` pairing above.

A point carries a session-identifying attribute at all only when
`captureIdentifiers` is on -- the same gate as task_id above -- so a
corpus without `captureIdentifiers` gets no tier-2 cost data, ever, not
an occasional gap; tier 1 is unaffected by this gate. Neither tier is an
exactly metered per-call figure, and `tool_call`/`tool_result` records
never get one either way.

## No ancestor-chaining needed -- a genuinely flatter shape

`sources.openclaw` needs a sibling-chaining fallback
(`_resolve_kept_ancestor_span_id`) because that exporter's real
`parent_span_id` topology couldn't be confirmed trustworthy from source
and tests alone. `openclaw-localtrace`'s topology is real and direct by
construction: every `model.call`/`llm.call`/`tool.execution` span is
literally a child of its `run` span, confirmed against a live capture.
There is no nested-call topology in this plugin's v1 scope to represent,
so `parent_id` is simply `None` for every record this adapter produces --
genuinely simpler, not a limitation.

## Three real bugs, found only by running it

None were visible from this plugin's or adapter's own unit test suites
alone -- each needed either a live Gateway and real agent turns, or a
real user's own multi-step capture, to surface:

1. Every hook fired correctly, but the runtime state they read from was
   always empty, so nothing was ever written to disk -- root-caused to
   `register(api)` being invoked more than once per Gateway process,
   leaving whichever registration's hooks were actually live pointing at
   state nobody's service instance had populated. (Plugin bug, fixed.)
2. The `llm.call`/`model.call` ordering bug: `llm_input` fires before
   `model_call_started`, `llm_output` fires after `model_call_ended`.
   (Plugin bug, fixed.)
3. This adapter's own original 1:1 pairing assumption for `llm.call`/
   `model.call`, described above -- confirmed wrong only once a real
   user's capture contained a run with far more than one `model.call`
   inside it. Not visible from the smaller-scale live test that validated
   bug 2 above, which happened not to exercise more than two calls in one
   run. (Adapter bug, fixed here.)

The lesson repeated across all three: a fix validated against one live
capture is not the same as a fix validated against real, varied usage --
each of these looked correct until a bigger, messier real session
disagreed.

## No `redundo collect` step

`openclaw-localtrace` writes OTLP JSON directly to a local directory in
the exact naming convention `redundo collect` already uses
(`traces-*.otlp.json`, `metrics-*.otlp.json`) -- there is no network
exporter to speak of and nothing for `redundo collect` to receive. Point
`redundo adapt` at the plugin's own configured `outputDir` directly.

## Recommended setup

```bash
openclaw plugins install openclaw-localtrace
openclaw plugins enable openclaw-localtrace
openclaw config set plugins.entries.openclaw-localtrace.config.enabled true
openclaw config set plugins.entries.openclaw-localtrace.config.captureIdentifiers true
openclaw config set plugins.entries.openclaw-localtrace.config.captureContent true
openclaw config set plugins.entries.openclaw-localtrace.hooks.allowConversationAccess true
# restart the Gateway, then drive real turns, then:
redundo adapt "$(openclaw config get plugins.entries.openclaw-localtrace.config.outputDir)" \
  --summary | redundo analyze --format html > report.html
```

`captureIdentifiers` and `hooks.allowConversationAccess` are both real,
deliberate opt-ins -- read the plugin's own README before enabling them.
Without both, this source still works, but degrades exactly the way
docs/openclaw.md's exporter always does: trace-scoped `task_id`, no
`cost_usd`, opaque `content_hash`.
