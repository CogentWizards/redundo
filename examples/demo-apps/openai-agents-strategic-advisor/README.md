# Demo: a strategic advisor, built on the OpenAI Agents SDK

A real business-strategy agent, run against the real OpenAI Agents SDK
(`openai-agents` exports via OpenInference conventions,
`openinference-instrumentation-openai-agents`, converted by redundo's
`openinference` adapter, the same one Hermes uses). No mocking: two real
`Runner.run()` sessions, one with a real `handoff()` to a specialist
agent, run straight through `redundo adapt | redundo analyze`.

```bash
export OPENAI_API_KEY=sk-...
./run.sh
```

Needs `OPENAI_API_KEY` in your environment. Takes a minute or two.

## Why this demo exists

Two questions about redundo's `openinference` adapter that had only ever
been answered by reading `openinference-instrumentation-openai-agents`'s
own source, never confirmed against a real capture:

1. **Does the `agent.name`-attribute preference in `_workflow_of` matter
   here?** That preference was added after finding Google ADK's own
   AGENT-kind span names itself `"agent_run [<name>]"` (decorated) while
   the bare name lives in a separate `agent.name` attribute. OpenAI
   Agents SDK's own source sets both the span name *and* `agent.name` to
   the same value (`Agent(name=...)`), so the preference should be a
   no-op here, not a fix -- worth confirming on real telemetry rather
   than trusting the source read alone.
2. **Does OpenAI Agents SDK ever split one logical turn into a coarse
   "whole-turn" LLM-kind span plus a separate granular "per-call"
   LLM-kind span**, the shape a real Hermes capture confirmed (see
   [docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md)
   and the investigation that started this demo, in this repo's own
   history) and that caused some `llm_call` events to show no
   `cost_usd`? If it does here too, that's a general shape
   `openinference.py` needs to handle, not a Hermes-specific patch.

## What it does

**Session A** asks the root **Strategic Advisor** agent for market data
on a sector, which hands off to a **Market Analyst** specialist (a real
`handoff()`, not a tool call pretending to be one). It's then asked to
re-run the exact same lookup verbatim (an exact-repeat candidate pair)
and a related-but-different sector (a near-duplicate candidate pair) --
real, checkable tool calls, so the same corpus also confirms
candidate-pair detection works end to end against this source.

**Session B** stays with the Strategic Advisor alone, explicitly told
not to delegate, repeating the same verbatim-then-related pattern with
its own direct tool access -- a real "no handoff, no AGENT-kind ancestor
at all" case, to confirm the `"main"` workflow default.

Every session is wrapped in one `trace()` call deliberately:
`openinference-instrumentation-openai-agents` never sets
`gen_ai.conversation.id`/`session.id` (confirmed by reading its own
`_processor.py`, not assumed), so without a shared trace each turn would
land in its own `trace_id` and therefore its own `task_id`, losing all
cross-turn candidate-pair detection within a session.

`get_market_data` is a small, deterministic, in-process lookup table,
not a real API -- every "verbatim repeat" is byte-identical by
construction, and running this costs nothing beyond the model calls
themselves.

## What actually happens on real data, and why

**This demo found a real, more significant bug than either original
question.** A first live run crashed the script itself (`RunResult` has
no `current_agent` attribute -- that's only on `RunResultStreaming`,
the return type of `Runner.run_streamed`; `Runner.run()`, used here,
returns `RunResult`, whose equivalent is `last_agent` -- fixed), but not
before real telemetry had already flushed for the first turn. That
partial capture showed every record's `workflow` as `"turn"`, not
`"Strategic Advisor"`.

The raw spans explain why: the real, named `Agent(...)` span sits
**inside** two layers of unnamed, purely structural `CHAIN` spans --
`"turn"` (one per tool-use round) and `"Agent workflow"` (one per run)
wrap it from below and above. `_workflow_of` used to stop at the
*nearest* `AGENT`/`CHAIN` ancestor of an LLM/TOOL span, which is almost
always `"turn"`, not the real agent -- confirmed exactly that on this
capture. Fixed to walk the whole ancestor chain for one with a real
agent-name attribute, falling back to the nearest ancestor's own span
name only if none exists anywhere. Re-adapting the same partial capture
with the fix applied confirms it: every record now reads `workflow:
"Strategic Advisor"`, `metadata.workflow_basis: "agent_name_attribute"`.

This generalizes past this one source: the same walk-past-uninformative-
ancestors fix is what `gen_ai.agent.name` (a different attribute key
than `agent.name`) needed to correctly resolve Hermes's own subagent
spans too -- see
[docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md).

**The original wrapper-span question is closed too, with a full run.**
All 11 `llm_call` records across both sessions carry a real `cost_usd`
and a real `model` -- zero unpriced, zero missing, exactly the same
clean result the Google ADK sibling demo found. No sign anywhere of
Hermes's "coarse whole-turn wrapper span plus a separate granular
per-call span, both tagged LLM" shape. That's now two different real
OpenInference sources tested, neither showing it -- solid evidence the
wrapper-span issue is a Hermes-specific (`hermes-otel`'s own
instrumentation choice), not a general shape `openinference.py` needs
to guard against.

The full run also confirms candidate-pair detection and the real
handoff both work end to end against this source: `workflow` correctly
switches from `"Strategic Advisor"` to `"Market Analyst"` exactly at the
real `handoff()` call (step 5 in the captured trace, a
`tool_call`/`tool_result` pair literally named `"handoff to Market
Analyst"`), and the verbatim-repeat / related-sector turns land real
candidate pairs. The exact-repeat lands `unclassified`
(`get_market_data`'s own write status is never recorded, the same
`metadata.write` absence every tool-call-based source in this package
has by default -- not specific to OpenAI Agents SDK), and the
related-sector repeat correctly lands `recurring_pattern` (the two
sessions share no confirmed delegation link, since each is its own
`with trace()` block).
