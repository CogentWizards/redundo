# OpenInference adapter

This is the contract this adapter implements. It's written down separately
from the code so another implementation (a different language, a
streaming pipeline, whatever) can produce output that's actually
comparable to this adapter's output. Comparability requires identical
behavior, not just "close enough."

## task_id

Prefer `gen_ai.conversation.id` when present, or `session.id` (the same
concept under OpenInference's own separate attribute name; confirmed
real for Google ADK, whose own OpenInference instrumentation maps ADK's
session id onto `session.id` and never touches `gen_ai.conversation.id`
at all). Fall back to the OTLP trace ID. **Never synthesize a grouping
key beyond that.**

Resolved **per span, not once for the whole trace**:

- The span's own value, if it carries one (preferring
  `gen_ai.conversation.id` if a single span somehow carried both; not
  expected in practice, since these are one source's own choice of
  attribute name, not two independent signals that could disagree).
- Otherwise, the nearest ancestor's value, walking the real
  `parent_span_id` chain. OTel context propagation means it's common for
  only some spans, often just a root-ish one, to carry it at all.
- Otherwise, that span's own trace ID.

This used to be resolved once per trace (collect every span's value; if
the whole trace agreed on one, use it, otherwise fall back to the trace
ID as "ambiguous"). That was wrong for a real, confirmed shape: Hermes's
`delegate_task` tool spawns subagents whose spans land in the *same* OTel
trace as the parent conversation, each with their own distinct, genuinely
real session id stamped directly on their own spans. Under the old
whole-trace rule this read as "conflicting values, don't guess" and
collapsed the parent and every subagent into one task_id, silently
defeating `metadata.parent_task_id` (below), since there was no second,
distinct task left for it to point at. Per-span resolution has no
remaining "conflicting values" case to guess at: once every span answers
for itself, two spans reporting two different real ids is just two
different real tasks sharing a trace, not ambiguity.

Every emitted record's `metadata.task_id_source` is `"conversation_id"` or
`"trace_id_fallback"`, naming which of the two happened for that specific
record. This is what lets a downstream report state a coverage figure
("N% of records grouped by a real conversation id") instead of a reader
having to trust the grouping blindly. See redundo analyze's own report
for where this gets surfaced.

Why this matters more than it looks like it should: a fabricated grouping
key produces confidently wrong repeat counts, not a visible gap. Two
genuinely unrelated tasks grouped under a made-up shared ID will show
"repeats" that never happened. A trace-ID fallback that's honestly
reported is a visible, explainable degradation instead, worse recall,
not wrong data.

**A task_id's own step numbering can span more than one physical trace.**
A source that gives one CLI invocation its own trace per turn but a
stable session id across turns (confirmed for Hermes: five separate
`hermes --resume <session-id> -z "..."` invocations, five OTel traces,
one real session id) needs its `step_index` sequence to stay one
continuous, collision-free count across all of them. Assigning
`step_index` per trace_id instead of per resolved task_id silently
collides two different real events onto the same `(task_id, step_index)`
key whenever a task spans more than one trace, corrupting
`redundo.analyzer.lineage.TaskLineage`'s step-indexed internals (`_by_step`,
`_effective_parent_step`) for every lineage method built on them. This
adapter groups kept spans by resolved task_id first, then runs the
step/chain-tail bookkeeping once per group, so this never happens
regardless of how many physical traces (or, within one trace, how many
distinct task_ids, the subagent case above) a task is assembled from.

## Cross-task links

`task_id` groups events within one conversation; it never crosses into a
different task, even when that other task is a subagent this one
delegated to. `metadata.parent_task_id` is a separate, additive signal
for exactly that case: the `task_id` of another task this one was
delegated from, set only when the source itself reports a real
delegation relationship, never inferred from timing, content, or
anything else.

Resolved the same way `task_id` is: the span's own
`hermes.subagent.parent_session_id` if present, else the nearest
ancestor's, walking the real `parent_span_id` chain. This ancestor walk
matters in practice, not just in theory. Confirmed against a real
capture, the attribute lives on a subagent's own wrapping AGENT-kind span
(never converted into an Event itself, see "Span kind to event type"
below), not on the LLM/TOOL spans nested inside it that actually need to
carry it forward.

`redundo.analyzer.task_graph.build_task_graph` reads this key to build a
task-level graph across the whole corpus (union-find over the
`parent_task_id` edges), and `WasteAnalysis` uses it to tell two
otherwise-identical findings apart: a same-or-similar call across a
*confirmed* task boundary lands in the `cross_task_redundancy` bucket, a
same-or-similar call across tasks with no confirmed relationship lands
in `recurring_pattern` instead, a frequency observation, never a waste
claim. See `cross_task_candidates.py`'s own module docstring for how
that search works without ever re-litigating a same-task decision
`cycles.py`/`near_duplicates.py` already made.

Per-source status, checked directly against each product's own source,
not assumed from docs:

| Source | Status |
|---|---|
| Hermes | done end to end, confirmed against a live `delegate_task` capture: `hermes-otel` emits `hermes.subagent.parent_session_id` on every subagent's own AGENT-kind span, read via the ancestor walk above, and (since the task_id-per-span fix) each subagent gets its own distinct task_id for it to point at. `cross_task_redundancy` genuinely fires on real data |
| OpenClaw | blocked upstream: its own internal session state tracks a comparable `parentSessionKey`, but it isn't on any hook payload a plugin can read today |
| Claude Code / Claude Agent SDK | in-process subagent delegation is already real span nesting under the parent `Task` call, no separate link needed at all; separate CLI processes per agent have no known signal |
| OpenAI Agents SDK | in-trace handoffs already work via the existing OpenInference translator's own span reparenting, no link needed; cross-trace linking (`group_id`, the SDK's own mechanism) is dropped by that same translator today, an upstream fix, not this adapter's to make |
| Google ADK | subagent delegation via `AgentTool` explicitly reuses the parent's own session id (confirmed in `openinference-instrumentation-google-adk`'s source), so it needs no separate link type at all |

## Span kind to event type

Only `openinference.span.kind` values of `LLM` and `TOOL` are converted.
Everything else (`CHAIN`, `AGENT`, `RETRIEVER`, `EMBEDDING`, absent) is
skipped and counted, not guessed at, none of them map cleanly onto
`llm_call` / `tool_call` / `tool_result`.

| OpenInference kind | Produces |
|---|---|
| `LLM` | one `llm_call` record |
| `TOOL` | one `tool_call` record, plus one `tool_result` record if `output.value` is present |
| anything else | nothing; counted in the conversion summary |

A `TOOL` span with no `input.value` is dropped entirely (no arguments, no
candidate for repeat detection). A `TOOL` span with `input.value` but no
`output.value` still produces its `tool_call` record, the analyzer's
result-identity signal for it will correctly read as unknown.

## Workflow and model

`workflow` is the nearest `AGENT`/`CHAIN` ancestor's own `agent.name`
attribute when present, else that span's own name, else `"main"` when no
such ancestor exists at all (a real fact -- this event ran at the top
level of the task, not inside a delegated sub-workflow -- not a guess).
`agent.name` is checked first, not the span's own name, because at least
one real, confirmed source decorates it:
`openinference-instrumentation-google-adk`'s own `AGENT`-kind span is
named `"agent_run [<name>]"` while carrying the bare, human-chosen name
in `agent.name` instead (confirmed by reading `_wrappers.py`'s real
source: `attributes[SpanAttributes.AGENT_NAME] = instance.name` is set
independently of the span's own decorated `name`).
`openinference-instrumentation-openai-agents` happens to set both to the
same value (`Agent(name=...)`), so this preference is a no-op there, not
a regression. `metadata.workflow_basis` states which of the three won
(`"agent_name_attribute"`/`"span_name"`/`"no_workflow_ancestor"`).

`model` is a real, call-level fact on `llm_call` records (`llm.model_name`/
`gen_ai.request.model`, read directly off the span), never approximated.
`TOOL`-kind spans carry no model attribute in any confirmed source --
a tool call isn't itself a model invocation -- so this adapter derives
one instead: the model of the nearest preceding `LLM`-kind span **in the
same task and workflow**, in true chronological order (`task_spans` is
processed in one start-time-sorted pass per task; a running "current
model" is tracked per resolved workflow, not one task-wide value,
specifically so one `AGENT` branch's model can never leak onto a sibling
branch's tool call just because its own `LLM` span happens to come first
chronologically -- see the cross-task/branch tests in
`tests/adapter/test_openinference.py`). `metadata.model_basis =
"preceding_llm_call"` marks this explicitly. `None`, with no
`model_basis` key, only for a `TOOL` span that happens before any `LLM`
span at all in its workflow -- genuinely nothing to derive from yet.

## Lineage (`parent_id`)

Walks the real OTLP `parentSpanId` chain, transparently skipping
non-kept-kind ancestors to find the nearest one that was actually
converted. A `tool_result` record's `parent_id` is always its own
`tool_call`'s step index. If a kept span has no kept ancestor at all,
`parent_id` is `None`, the analyzer's own linear-fallback default
applies from there, not a guess made here.

## Content hashing

Shared by every source, not implemented per-source. See
[docs/hashing.md](hashing.md) for the full procedure (algorithm,
normalization, masking order, versioning, and a copyable, about
15-line, reference implementation).

## Cost estimation

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

## Split content and tokens

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
