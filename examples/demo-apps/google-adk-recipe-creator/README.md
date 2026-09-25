# Demo: a recipe creator, built on Google ADK

A real recipe-writing agent, run against the real Google Agent
Development Kit (`google-adk` exports via OpenInference conventions,
`openinference-instrumentation-google-adk`, converted by redundo's
`openinference` adapter, the same one Hermes uses). No mocking: two real
ADK sessions, one with real sub-agent delegation (`sub_agents=`), run
straight through `redundo adapt | redundo analyze`.

```bash
export GOOGLE_API_KEY=...
./run.sh
```

Needs `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) in your environment. Takes
a minute or two.

## Why this demo exists

Two questions about redundo's `openinference` adapter that had only ever
been answered by reading `openinference-instrumentation-google-adk`'s
own source (`_wrappers.py`), never confirmed against a real capture:

1. **Does the `agent.name`-attribute preference in `_workflow_of`
   actually matter here?** Confirmed from source: ADK's own AGENT-kind
   span is named `f"agent_run [{instance.name}]"` (decorated) while
   `attributes[SpanAttributes.AGENT_NAME] = instance.name` carries the
   bare name separately. Without preferring the attribute, a report's
   "by workflow" column would show `"agent_run [nutrition_analyst]"`
   instead of `"nutrition_analyst"` -- this demo confirms that's a real
   fix, not a defensive no-op.
2. **Does ADK ever split one logical turn into a coarse "whole-turn"
   LLM-kind span plus a separate granular "per-call" LLM-kind span**,
   the shape a real Hermes capture confirmed (see
   [docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md)
   and the investigation that started this demo, in this repo's own
   history) and that caused some `llm_call` events to show no
   `cost_usd`? If it does here too, that's a general shape
   `openinference.py` needs to handle, not a Hermes-specific patch. From
   source, `_wrap_call_llm.py` sets `LLM_MODEL_NAME` directly on what
   looks like the one real completion span, suggesting ADK may not have
   this split at all -- worth confirming rather than assuming.

## What it does

**Session A** asks the root **Recipe Creator** agent for nutrition data
on an ingredient, which delegates to a **nutrition_analyst** sub-agent (a
real ADK `sub_agents=` delegation, not a tool call pretending to be
one). It's then asked to re-check the exact same ingredient verbatim (an
exact-repeat candidate pair) and a related-but-different ingredient (a
near-duplicate candidate pair) -- real, checkable tool calls, so the
same corpus also confirms candidate-pair detection works end to end
against this source.

**Session B** stays with the Recipe Creator alone, explicitly told not
to delegate, repeating the same verbatim-then-related pattern with its
own direct tool access -- a real "no delegation, no AGENT-kind ancestor
at all" case, to confirm the `"main"` workflow default.

Each session is its own ADK session (a real `session.id`, already
confirmed mapped onto the schema's `session.id` attribute -- never
`gen_ai.conversation.id` -- by this adapter's own docs, itself confirmed
by reading `openinference-instrumentation-google-adk`'s source). ADK's
session concept already spans multiple `run_async` turns natively,
unlike the OpenAI Agents SDK sibling demo, so no manual trace-grouping
workaround is needed here.

`get_nutrition_facts` is a small, deterministic, in-process lookup
table, not a real API -- every "verbatim repeat" is byte-identical by
construction, and running this costs nothing beyond the model calls
themselves.

## What actually happens on real data, and why

Both open questions above got real, confirmed answers from a live run.

**The `agent.name` preference is real, not a defensive no-op.** The raw
capture's `AGENT`-kind spans are literally named `agent_run
[nutrition_analyst]` and `agent_run [recipe_creator]` -- exactly as
`_wrappers.py`'s source predicted -- while `agent.name` carries the bare
`nutrition_analyst`/`recipe_creator`. Every one of the 11 `llm_call`
records in this capture resolved its `workflow` from that attribute
(`metadata.workflow_basis = "agent_name_attribute"`), correctly
switching between `recipe_creator` (the root agent's own calls) and
`nutrition_analyst` (the sub-agent's calls) call by call, including the
sub-agent's every completion inside Session A's delegation. Without the
preference, the report's "by workflow" column would show the decorated
`agent_run [...]` form instead.

**No sign of Hermes's wrapper-span shape here.** All 11 `llm_call`
records got a real `cost_usd` and a real `model` -- zero unpriced,
zero missing. ADK's own `_wrap_call_llm.py` sets `LLM_MODEL_NAME`
directly on what is genuinely the one real completion span per call, not
a coarse whole-turn wrapper the way hermes-otel's `llm.<model>` span is.
That confirms the wrapper-span double-counting issue found on a real
Hermes capture (see
[docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md))
is a Hermes-specific instrumentation choice, not a shape
`openinference.py` needs to guard against generally -- redundo doesn't
need a broad, riskier fix here.

Building this demo also surfaced a real bug that had nothing to do with
either original question: `_workflow_of` used to stop at the *nearest*
`AGENT`/`CHAIN` ancestor rather than walking past uninformative ones.
That never mattered for ADK's own shape (its `AGENT`-kind span is a
direct parent of the LLM span, no structural wrapper in between,
confirmed by this same capture), but it broke the OpenAI Agents SDK
sibling demo badly enough to be worth fixing generally -- see that
demo's own README for the full story.
