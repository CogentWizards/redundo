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

## What it does

**Session A** investigates a real failing test, then deliberately
re-runs the exact same check once more before stopping, without fixing
anything: identical arguments, identical (failing) result, nothing
written in between. A human skimming this transcript would call it
`confirmed_waste` without a second thought. On real captured data it
lands in `unclassified` instead, and that's deliberate, not a bug in the
demo: redundo requires the *trace itself* to confirm the task ended in
failure, not just the call-level repeat, and the session's last recorded
event is Claude's own closing message, which succeeds as an API call
regardless of what it says. redundo won't borrow "a human would call
this waste" to fill that gap; see the `unclassified` reason string this
run prints for the exact signal that's missing. That refusal to guess is
the entire point of the tool, and this is a real, unforced example of it
holding the line.

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
What's still an open, tracked limitation is the deeper reason Session A
lands in `unclassified`: see the follow-up task on preserving a tool
call's real outcome even when its content can't be captured.
