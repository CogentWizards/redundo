# Demo: a code review bot, built on the Claude Agent SDK

A real, minimal "review this PR" agent, the single most common thing
people build with the Claude Agent SDK, run twice against a tiny fixture
repo with one deliberate bug. No mocking: two real Claude sessions, real
tool calls, real telemetry, run straight through `redundo adapt | redundo
analyze`.

```bash
./run.sh
```

First run sets up a local venv with the Claude Agent SDK (`pip install
claude-agent-sdk`); needs `claude` on your `PATH` and you logged in
already. Takes a couple of minutes end to end.

## The finding that actually matters here

Session A below does something almost any "agent waste" tool would flag
without a second thought: it repeats a failing check verbatim and stops.
redundo doesn't flag it as `confirmed_waste`. Not because it missed the
repeat, it sees the repeat clearly: identical arguments, identical
result, nothing written in between. It holds back because Claude Code's
own telemetry has no way to say "the task itself failed," only "the API
call succeeded," and redundo refuses to promote a human's confident read
of the transcript into a verdict the trace itself can't support. See
[docs/schema.md](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md#the-six-buckets)
for why `unclassified` is a real answer here, not a gap in this tool.

That's the actual pitch, spelled out in the main README's [Not a tracing
platform](https://github.com/CogentWizards/redundo/blob/main/README.md#not-a-tracing-platform)
section: a tracing UI would show you this exact repeat and let you draw
whatever conclusion you want from it. redundo adjudicates under a stated
evidence rule instead, and says so plainly when the rule can't be met.
Nobody else ships that abstention. This run is a live, unscripted example
of it holding the line, not a best-case demo picked to make the tool look
good.

## What it does

**Session A** investigates a real failing test, then deliberately
re-runs the exact same check once more before stopping, without fixing
anything: identical arguments, identical (failing) result, nothing
written in between. On real captured data this lands in `unclassified`,
for the reason above; see the `unclassified` reason string this run
prints for the exact signal that's missing.

**Session B** investigates the same failure, researches the correct fix
online (twice, rephrased slightly the second time), fixes the bug, and
re-verifies. The two research calls are similar, not identical
(`near_duplicate`); the fix-then-recheck loop is a real write followed by
a real result change (`likely_legitimate`).

Both sessions are real, separate Claude Code sessions (separate
`task_id`s). Depending on the run, their opening investigation steps can
also land in `recurring_pattern` if redundo's cross-task search notices
they happen to look similar, since nothing links these two sessions and
it isn't one, correctly reported as a coincidence, not a relationship.

## Why this shape, and what's real here

Every number in this README came from actually running the app against
a live `redundo collect` receiver and reading the real `redundo analyze`
output, not from designing the scenario and assuming it would land where
intended. It didn't, the first few times: the initial version relied on
the model discovering a missing `pytest` install mid-session (a realistic
detour, but one that made every "identical" repeat call slightly
different), and a real Claude Code telemetry quirk (a failing Bash call's
output is invisible to OTLP unless the shell command itself exits 0)
meant the intended `confirmed_waste`/`likely_legitimate` pairs kept
missing a required signal. Fixing that surfaced two real gaps in
redundo itself, now merged: the `claude-code` adapter never populated
`similarity_fingerprint` (so `near_duplicate` could never fire for this
source at all), and a task's synthesized cost-only record (Claude Code's
own session-title-generation call) was silently blanking out
`terminal_outcome` on every real capture that had one. Both are fixed.

What's left, and won't be "fixed" the same way: a Claude Code session's
last recorded event is always the model's own closing reply, an API call
that succeeds regardless of what it says, so `terminal_outcome` (see
[docs/schema.md](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md#design-decisions))
can never read "failure" for a normal session. That's a structural
property of Claude Code's own telemetry, confirmed independently on a
second, unrelated source (see the `openclaw-daily-briefing` demo's own
README), not a bug redundo can patch without inventing a signal the
trace doesn't actually provide.
