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

## What it does

**Session A** asks for a research topic and lets the agent delegate it
to two subagents via Hermes's real `delegate_task` tool -- a genuine,
overlapping-topic, concurrent multi-agent workflow, and the flagship
scenario this app was built around. The two subagents run *in parallel*,
as lineage siblings, not one descended from the other, so redundo
correctly never flags their overlapping calls against each other in
same-task matching -- that would mean treating normal parallel fan-out
as waste, which it isn't. Catching genuinely redundant parallel work is
exactly what `cross_task_redundancy` exists for, and it needs two real,
distinct task_ids linked by a confirmed delegation edge to have a chance
to fire.

That used to be broken for real Hermes captures (redundo's `openinference`
adapter collapsed a parent conversation and its subagents into one
task_id, and separately lost the subagent-parent link on top of that --
both bugs found and fixed while building this demo, see
[CogentWizards/redundo#31](https://github.com/CogentWizards/redundo/pull/31)).
With the fix merged, this session's output now genuinely populates
`cross_task_redundancy` on a live run: both subagents independently look
up the same skill documentation before starting their own research, and
redundo correctly links that repeat back to the shared `delegate_task`
call that spawned them -- confirmed related, not a guess from timing or
content.

**Session B** is a single, sequential turn: calculate something, repeat
the exact same calculation verbatim (nothing intervening), save the
result to a file, repeat once more to double-check, then a nearby but
different calculation. This one *is* fully same-task, sequential, no
parallelism -- and produces its own real finding: the two verbatim-repeated
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
