# Demo: a research assistant with delegation, built on Hermes

A real research agent, run against real Hermes telemetry (Hermes exports
via OpenInference conventions, converted by redundo's `openinference`
adapter). No mocking: two real, single-invocation `hermes -z` runs, real
tool calls including Hermes's own `delegate_task` subagent tool, run
straight through `redundo adapt | redundo analyze`.

```bash
./run.sh
```

Needs `hermes` on your `PATH`, already logged in with a working model
provider, and `hermes-otel` already configured to export to
`http://localhost:4318/v1/traces` (see `~/.hermes/hermes_otel.yaml` --
that endpoint is fixed by Hermes's own config, not overridable per run,
so this script owns port 4318 for its duration). Takes a couple of
minutes.

## What it does, and what's real vs. still tracked

**Session A** asks for a research topic and lets the agent delegate it
to two subagents via Hermes's real `delegate_task` tool -- a genuine,
overlapping-topic, concurrent multi-agent workflow. This is the
scenario this app was built around, and it's a **real, honest look at
where redundo's multi-agent story is today, not a finished
demonstration**:

- The two subagents run *in parallel*, as lineage siblings, not one
  descended from the other. redundo correctly never flags siblings'
  overlapping calls against each other in same-task matching -- that
  would mean treating normal parallel fan-out as waste, which it isn't.
  Catching genuinely redundant parallel work is exactly what the
  `cross_task_redundancy` bucket exists for.
- For `cross_task_redundancy` to have a chance to fire, each subagent
  needs its own task_id, linked back to the parent's. Right now it
  doesn't: verified directly against a live capture, Hermes puts the
  parent conversation and both subagents' spans in one OTel trace, each
  with a different real session id, and redundo's `openinference`
  adapter's task-id resolution currently treats "more than one
  conversation id in one trace" as ambiguous and falls back to grouping
  everything under one task_id. There's a tracked follow-up for this.
- A second, related gap: the attribute that names a subagent's parent
  (`hermes.subagent.parent_session_id`) lives on a wrapper span this
  adapter currently skips entirely, so even independent of the above,
  it isn't reaching the events that would need it. Also tracked.

So Session A runs for real and its output is real, but it does not
currently populate `cross_task_redundancy` -- treat it as this app
building toward its flagship scenario, not yet delivering it.

**Session B** is a single, sequential turn: calculate something, repeat
the exact same calculation verbatim (nothing intervening), save the
result to a file, repeat once more to double-check, then a nearby but
different calculation. This one *is* fully same-task, sequential, no
parallelism -- and does produce a real finding: the two verbatim-repeated
calculations exact-match on the call side, but classify as
`likely_legitimate` ("result changed") rather than `confirmed_waste`,
because the tool's own result payload carries a small amount of
per-call execution metadata alongside the actual answer, which isn't
byte-identical between calls even though the answer is. This is the same
family of finding as the `openclaw-daily-briefing` demo's README
documents in more depth (a wrapper carrying volatile per-call data
defeats exact result-identity comparison) -- seeing the same shape of
gap independently on a second, unrelated source is itself a useful
signal about where redundo's masking needs to get more thorough.

## Why single-invocation sessions, not multi-turn `--resume`

An earlier version of this demo drove Session B as five separate
`hermes --resume <session-id> -z "..."` calls, one process (and one OTel
trace) per turn. That's a more natural shape for a real multi-turn
conversation, but it hit a real, separately-tracked bug: redundo's
`openinference` adapter numbers each trace's events starting from
step 0 independently, so when several traces share one real task_id (a
conversation spanning several CLI invocations, which is Hermes's normal
shape), different real events end up sharing the same `(task_id,
step_index)` key, and that silently corrupts the lineage index every
same-task candidate-pair search depends on -- a real repeat in that
capture produced zero candidate pairs at all. Keeping each session to
one `-z` call sidesteps it entirely (Hermes can and does call several
tools in sequence within a single turn, so nothing about the "multi-step
workflow" story is lost), while the underlying bug stays tracked for a
proper fix.
