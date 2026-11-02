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

## What actually happens on real data

Verified against a real run (OpenCode 1.19.5, `claude-sonnet-4-6`,
`hash_spec` v2, `similarity_spec` v3, 16-bit near-duplicate threshold):
4 sessions (one of them the subagent's), 13 model calls, 9 tool calls,
$0.3249, every model call priced by OpenCode itself.

**Session A** lands in `unclassified`, reason: "result identical; no
intervening write; task succeeded, but that doesn't confirm this
specific repeated call contributed." The same honest limit as every
other source: the task ended in a normal reply, so redundo won't call
the repeat `confirmed_waste`. Its two model calls after the two
identical failing runs are a 0-bit `near_duplicate`, which is true: the
model was given exactly the same input twice. That's the only
`near_duplicate` in the run.

**Session B** lands in `likely_legitimate`, reason: "result changed;
write intervened". The write is real. OpenCode's snapshot recorded the
edit to `invoice.py` in a step that ran only the edit, so that call is
marked as a write (see
[docs/opencode.md](https://github.com/CogentWizards/redundo/blob/main/docs/opencode.md#writes)).
It's the only call marked as a write in the run.

**Session C** delegated the test run to the `general` subagent, which
became its own session linked back to C as a delegation. The model
didn't quite follow orders on the double-check: it dropped the `2>&1`,
so its own run isn't the same call as the subagent's. Two of its model
calls land in `cross_task_redundancy` with the subagent's: its call
after the subagent reported back is 13 bits from the subagent's first
call, which got the same instructions, and its call after its own run
is 8 bits from the subagent's call after the same passing output.

The subagent's run is an exact match to Session B's first run, but
those two tasks aren't linked, so it's a `recurring_pattern`, not a
finding. There are six `recurring_pattern` pairs in all. Most are the
same command with a different test class. Two are borderline, at 15
and 16 bits: Session A's and Session B's first model calls (similar
prompts) and their calls after a failing test (two different
failures).

Building this demo found three real problems:

- OpenCode 1.19 stores each step of a turn as its own assistant
  message, not one message per turn as its schema suggests. Every model
  call in a session got the same similarity fingerprint (the user's
  message) until that was fixed.
- Python writes `__pycache__` when the tests run, and OpenCode's
  snapshot recorded that as a file change, marking the test runs as
  writes. The demo now sets `PYTHONDONTWRITEBYTECODE=1`.
- `unittest`'s 70-character separator lines outvoted the text in a
  fingerprint, so an unrelated passing report and a failing test came
  out 0 bits apart. `similarity_spec` v2 counts each distinct shingle
  once (see
  [docs/hashing.md](https://github.com/CogentWizards/redundo/blob/main/docs/hashing.md#similarity-fingerprinting)).
