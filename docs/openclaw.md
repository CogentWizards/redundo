# Adapter spec: OpenClaw's `@openclaw/diagnostics-otel` -> the redundo.analyzer Event schema

This adapter is built against the exporter's own TypeScript source
(`extensions/diagnostics-otel/src/*.ts` in the [openclaw/openclaw](https://github.com/openclaw/openclaw)
repo) and its own test suite's literal example payloads, not against
documentation prose alone -- see "A live capture attempt" below for why.

## Span kinds

OpenClaw's exporter produces several span names; only two become Events:

| Span name | Becomes |
|---|---|
| `openclaw.model.call` | `llm_call` |
| `openclaw.tool.execution` | `tool_call` (+ `tool_result` when output was captured) |
| `openclaw.harness.run`, `openclaw.run` | nothing -- structural wrapper spans, used only for lineage-ancestor lookup and the `workflow` label |
| `openclaw.model.usage` | nothing -- see "The `openclaw.model.usage` span" below |
| everything else (`openclaw.session.stuck`, `openclaw.tool.loop`, `openclaw.memory.pressure`, ...) | nothing, unrelated to call/tool-call accounting |

Under `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, model-call
spans are named `"<gen_ai.operation.name> <model>"` instead of
`openclaw.model.call` -- genuinely ambiguous with other gen_ai-semconv
sources by name alone, so `detect.py` falls back to the
`openclaw.model_call.observation_unit` attribute, which the exporter sets
on every model-call span regardless of naming mode.

### The `openclaw.model.usage` span

The exporter's source builds this as a span distinct from
`openclaw.model.call`, from a separate `model.usage` diagnostic event. This
adapter could not confirm, from the available source and tests alone,
whether it ever represents an API call not already captured by
`openclaw.model.call`, or is always a redundant re-emission of the same
call's usage. Converting it as a second `llm_call` risked double-counting;
skipping it risks a blind spot if it's ever the *only* signal for some call
shape. Skipped, not guessed at -- resolve this against a real capture
before changing it (see "A live capture attempt").

## task_id: always the trace ID, not a fallback -- a ceiling

Every other OTLP source this project supports has a "real conversation id,
falls back to trace id" story, with a documented cost to the fallback.
OpenClaw does not have that story at all: its exporter **actively strips**
every session/run/call identifier from every exported span before export.
Verified directly in the exporter source, not inferred:

- `addRunAttrs` (the function that copies event fields onto span
  attributes) is typed to accept `runId`, `sessionKey`, and `sessionId`,
  but its body never reads them into the output attribute set.
- Every span attribute set additionally passes through a deny-list scrub
  (`redactOtelAttributes` / `DROPPED_OTEL_ATTRIBUTE_KEYS`) that drops
  `openclaw.sessionKey`, `openclaw.sessionId`, `openclaw.runId`,
  `openclaw.callId`, `openclaw.toolCallId`, and their snake_case aliases,
  even if some future code path put one in by mistake.
- The exporter's own test suite asserts this directly: a test that feeds
  `sessionKey`/`sessionId` into the underlying events explicitly checks
  `Object.hasOwn(...).toBe(false)` for every one of these keys on the
  exported span attributes.

The only thing that *does* survive is native OTel trace/span-id parent-child
linkage (not an `openclaw.*` attribute -- the actual span context), plus
`gen_ai.tool.call.id`, a deliberate per-*call* semconv identity kept for
compatibility with generic OTel viewers. Neither is a session key.

**Consequence:** `task_id` for this source is always the OTLP trace id.
`metadata.task_id_source` is still set to `"trace_id_fallback"` (the
existing convention's value across every source for "not a real
conversation id"), but the framing that value usually carries -- "this
corpus degraded from a better signal" -- doesn't quite fit here. There is
no better signal to have degraded *from*; trace-id-scoped grouping is the
most this signal can ever give for this source, by design, not by gap.
Repeats spanning more than one OTel trace (almost certainly: more than one
turn in the same OpenClaw session) are structurally invisible to this
adapter. If OpenClaw's trace boundaries turn out to be per-turn rather than
per-session (plausible from the harness/run span hierarchy documented in
OpenClaw's own OTel guide, but not directly confirmed here), that makes
this adapter's `task_id` scoped to a single turn, not a whole conversation
-- narrower coverage than the other three sources give.

## Lineage

Real `parent_span_id` walked the same way as `sources.openinference`:
`openclaw.harness.run` -> `openclaw.run` -> `openclaw.model.call` /
`openclaw.tool.execution` gives genuine parent-child structure when
present. Whether OpenClaw actually nests sequential calls under each other,
or puts every call in one turn as flat siblings of a single `openclaw.run`
span (confirmed to happen for at least one other real OTel-instrumented
agent framework -- see `sources.openinference`'s own docstring), was not
verified against a live capture (see below). This adapter includes the
same interval-based sibling-chaining safety net `sources.openinference`
uses as a precaution either way: a no-op if the real topology already
nests correctly, a safety net if it doesn't, but only beneath a real
*kept* ancestor further up the chain -- a flat group with nothing kept
above it (e.g. the first turn in a trace) is left unlinked rather than
guessed at, since chaining across two genuinely independent top-level
flame graphs in the same trace would be a wrong finding, not a
conservative one.

`workflow` comes from the nearest `openclaw.harness.run` ancestor's
`openclaw.harness.id` attribute (e.g. `"claude-cli"`, `"embedded"`),
falling back to the span's own `openclaw.channel` attribute, falling back
to `None`.

## Content: opt-in, and off changes what's even hashable

Raw content (`gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`) is exported only
when the operator sets `diagnostics.otel.captureContent: true` -- off by
default. The message-shape verified against the exporter's own test
assertions: `gen_ai.input.messages`/`gen_ai.output.messages` are a single
JSON-stringified array attribute, one object per message,
`{role, parts: [...], finish_reason?}`, each part one of
`{type: "text", content}` / `{type: "tool_call", id?, name, arguments?}` /
`{type: "tool_call_response", id?, response}` / `{type: "blob", ...}`.

Without `captureContent`, every record from this source degrades to
`content_basis: "opaque"` -- a hash of the span's own id (never
coincidentally matching another record's hash), the same idiom
`sources.claude_code` uses for its own no-content case. This means a
corpus captured with the default configuration produces records that load
and count correctly (coverage, cost-shape, task counts) but can never
participate in a candidate pair -- every `llm_call`/`tool_call` looks
unique by construction. That's not a bug in this adapter; it's an honest
reflection of what the operator chose to export.

## Cost: unreachable from the signals this adapter reads, not merely unobserved

OpenClaw's exporter does compute and export a real cost estimate --
`openclaw.cost.usd`, a Counter metric, fed from `model.usage` diagnostic
events whenever they carry a `costUsd` value. But it is **only** exported
on the metrics OTLP signal, never as a span attribute on `openclaw.model.call`
or anywhere else. This package's adapters only ever read the traces and
logs signals (matching how `redundo collect` only serves `/v1/traces`
and `/v1/logs`); this source's logs signal, separately, carries only
generic gateway log and security-event records -- nothing model-call-shaped,
unlike `sources.claude_code`'s `api_request` log record, which is where
*that* source's real cost comes from. `cost_usd` is therefore always
`None` from this adapter, structurally, not because no corpus has
happened to carry it yet.

## `metadata.write` is never set

No signal anywhere in the exporter's event handling indicates whether a
tool call had a side effect. The only tool-identity fields available at
all are `toolName`, `toolSource`, `toolOwner`, `toolCallId`, and a
params-shape summary (`kind`/`length`, not real content). Same policy as
`sources.claude_code`: inferring "write" from a tool name would be a guess
dressed as a finding, so this stays `Signal.UNKNOWN` for every record from
this adapter.

## Blocked tool calls

A tool call OpenClaw's own policy denies before it executes carries
`openclaw.outcome = "blocked"` and never produces output -- there will
never be a `tool_result` for it, unlike an ordinary call whose result
simply wasn't captured. Rather than let that read identically to "result
unknown," this adapter puts `outcome: "error"` directly on the `tool_call`
record itself for this one case -- the only place that real signal can go.

## A live capture attempt

Before writing this adapter, a real OpenClaw Gateway was set up end to end:
an isolated profile, a local Ollama model (no external API keys or cost),
`@openclaw/diagnostics-otel` installed and enabled, `captureContent` and
`sampleRate` both forced on, `redundo collect` listening. `openclaw
doctor` confirmed the traces and logs exporters connected successfully,
and `sessions list` confirmed the driven turns genuinely executed through
the instrumented Gateway process (real token usage recorded). Every
exported batch across several turns was nonetheless empty -- zero spans,
zero log records.

Reading the exporter's own source ruled out the two most likely causes:
no provider-name gate exists anywhere in the plugin (grepped exhaustively),
and the plugin's own trust-filtering logic (`metadata.trusted`) only
affects whether a `*.started` span gets a real trace parent or a rootless
one -- it does not produce zero spans either way, confirmed by the
exporter's own test for exactly this case. The most likely explanation is
that the diagnostic event a `model.call.*`/`tool.execution.*` recorder
needs was never emitted for this specific embedded/Ollama-provider code
path -- code outside the `diagnostics-otel` plugin itself, not available
to inspect from this package's checkout.

This adapter is therefore built against the exporter's own source and
test-asserted example payloads (a form of "real data," just not a live
capture), rather than against documentation prose. Anyone who can get a
genuine live capture -- with a real hosted provider, or once the empty-export
issue above is understood -- should treat that as the next validation
step, the same discipline every other source in this package was held to.
See CONTRIBUTING.md.

## Recommended setup

```bash
openclaw plugins install clawhub:@openclaw/diagnostics-otel
openclaw plugins enable diagnostics-otel
openclaw config set diagnostics.enabled true
openclaw config set diagnostics.otel.enabled true
openclaw config set diagnostics.otel.endpoint "http://localhost:4318"
openclaw config set diagnostics.otel.captureContent true   # opt-in; see "Content" above
# restart the Gateway, then:
redundo collect --out-dir ./otlp_traces &
# ... drive real turns through the Gateway ...
redundo adapt ./otlp_traces --summary | redundo analyze --format html > report.html
```
