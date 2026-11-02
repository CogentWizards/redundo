# Demo: a bug fixer, built on OpenCode

Three real [OpenCode](https://github.com/anomalyco/opencode) sessions
against the same tiny invoicing repo as the [Pi demo](../pi-bug-fixer/):
one real bug, and one test that can't pass from inside the repo. No
collector and no instrumentation. OpenCode already stores every session,
and `redundo adapt` reads `opencode export` output directly.

```bash
./run.sh
```

Needs `opencode` 1.x on your `PATH` (`npm install -g opencode-ai`),
already set up with a working model provider. Pick the model with
`OPENCODE_DEMO_MODEL=provider/model ./run.sh`; unset, OpenCode uses your
configured default. The sessions run with `--auto` (tool permissions
approved without asking) inside a throwaway git copy of the fixture, and
only this run's sessions are exported, found by that copy's directory.
They also stay in your OpenCode history, titled `redundo demo A/B/C`.
Takes a minute or two.

## What it does

**Session A** runs a test that reads a fixture file from outside the
repo, which doesn't exist, so it can't pass. It's told to re-run the
exact same command once to rule out flakiness, then stop without
touching anything.

**Session B** runs the line-total tests, fixes the real bug in
`invoice.py` (the discount is applied as a fraction when it's a
percentage), then re-runs the same command to confirm.

**Session C** hands the same test run to OpenCode's `general` subagent
through the `task` tool, then runs it again itself to double-check.

## What to look for

- **Session A**'s repeat is the clean "stuck" case: identical call,
  identical failing result, nothing written in between. As with every
  source, the task ending in a normal reply means redundo won't call it
  `confirmed_waste`.
- **Session B**'s repeat should read as `likely_legitimate`: the result
  changed. If OpenCode's snapshot recorded the edit in a step that ran
  only the edit, that call is also marked as a write (see
  [docs/opencode.md](https://github.com/CogentWizards/redundo/blob/main/docs/opencode.md#writes)).
- **Session C**'s subagent runs in its own child session, linked to
  Session C. The parent's double-check repeats the subagent's run across
  two linked tasks, so it lands in `cross_task_redundancy`, not
  `recurring_pattern`. The subagent's records carry its own agent name
  as their workflow.
- Every `llm_call` is one step, with OpenCode's own per-step cost, and
  every tool event carries the model of the step that issued it.
