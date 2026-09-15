# Context drift hints: `redundo drift`

A heuristic, not a finding, and deliberately not part of `redundo analyze`'s
own report. This document says plainly what that means and why, so it's
never mistaken for the same kind of claim as a bucket.

## Why this is a separate command, not a bucket

Every bucket `redundo analyze` produces, even the "surfaced for review, not
a verdict" ones (`near_duplicate`, `cross_task_redundancy`), is a
deterministic, reproducible finding against a threshold that's at least
been checked against a real capture. `near_duplicates.DEFAULT_SIMILARITY_THRESHOLD`
came from an actual near-identical-URL test on real data, documented as
such. Nothing backs a drift threshold the same way: no `redundo` adapter
today emits the kind of link this needs (see below), so there is no real
corpus to calibrate against yet. Presenting this next to `confirmed_waste`,
even in its own bucket, would borrow credibility it hasn't earned.

It also doesn't fit the shape a bucket has. `Bucket`/`Slice` are built
around `cost_usd` and token counts, "how much did this cost." Drift isn't
a cost or waste claim at all, it's a structural observation about a chain
of tasks. Forcing it into that shape would misrepresent it a second way.

So: its own command (`redundo drift`), its own output (a plain list of
measured distances and a literal diff, not a verdict), and an explicit
disclaimer printed at the top of every run.

## What it needs: a continuation link, not a delegation link

`metadata.parent_task_id` (see `docs/openinference.md`) says two tasks
are related. It doesn't say *how*. `metadata.parent_task_link_kind`
narrows that to two kinds:

- **`"continuation"`**: this task is a later chapter of the *same*
  logical thread of work, a session resumed or rolled over. This is what
  drift needs: a real, ordered sequence of tasks representing one
  continuous conversation over time.
- **`"delegation"`**: this task was spawned *by* the parent as a distinct
  sub-agent. `hermes-otel`'s `hermes.subagent.parent_session_id` is this
  kind, the only real `parent_task_id` data any source emits today.

Walking a delegation edge and calling the distance "drift" would compare
an orchestrator to a subagent it spawned, two things that were never one
continuous thread to begin with, a category error, not a measurement.
`redundo drift` only ever walks edges explicitly marked `"continuation"`.
An unmarked `parent_task_id` (Hermes's, today) is never treated as one.

**No source currently reports a continuation-type link.** This command
is fully built and tested against synthetic data, but has no real corpus
to run against until one does. The natural candidate is OpenClaw's own
internal `previousSessionId` (see `docs/openinference.md`'s per-source
table), tracked there for the same reason: it already models "this
session id replaced that one," exactly the relationship a continuation
edge needs.

## What it measures, and what it deliberately doesn't blend together

For each hop between consecutive tasks in a continuation chain, two
independent numbers, never combined into one score:

- **Shape distance**: edit distance (Levenshtein) between the two tasks'
  own ordered call signatures, `(event_type, name)` in step order.
  Catches "the workflow now takes a different path," independent of what
  any one call's content looks like.
- **Content distance**: `hamming_distance` on `similarity_fingerprint`,
  computed only where the alignment says two calls actually correspond
  (never across a substitution, comparing a `tool_call`'s fingerprint to
  an `llm_call`'s would not be meaningful, the same restriction
  `near_duplicates.py` already applies within one task).

The alignment itself, not just its distance, is the more useful output:
it names the specific calls added, removed, or substituted between one
task and the next, a literal diff someone can check by hand, not a number
they have to interpret on faith. Two identically-shaped hops can still
differ in *why*: one might add a genuinely new step, another might
replace one step with a different one, `redundo drift`'s output
distinguishes these rather than collapsing both into "shape changed by 1."

## Reading the output

```
Chain: s1 -> s2 -> s3 (3 tasks)

  s1 -> s2:
    shape distance: 1 step(s) changed (33% of 3)
    content distance at matched steps: worst 32/64 bits (step 0 -> step 0)
    ~ substituted: tool_call/verify_output (step 2) -> tool_call/retry_search (step 2)
```

Read this as: task `s2` (a continuation of `s1`) replaced its
`verify_output` step with a `retry_search` step, and its opening call's
content diverged substantially from `s1`'s (32 out of 64 bits). Worth a
look, not a confirmed finding.

`--format json` gives the same data structured for scripting.
