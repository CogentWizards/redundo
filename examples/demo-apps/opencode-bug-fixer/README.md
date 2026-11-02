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

Verified against a real run (OpenCode 1.19.5, `claude-sonnet-4-6`): 4
sessions (one of them the subagent's), 13 model calls, 9 tool calls,
$0.3263, every model call priced by OpenCode itself.

**Session A** lands in `unclassified`, reason: "result identical; no
intervening write; task succeeded, but that doesn't confirm this
specific repeated call contributed." The same honest limit as every
other source: the task ended in a normal reply, so redundo won't call
the repeat `confirmed_waste`. Its two model calls after the two
identical failing runs are a 0-bit `near_duplicate`, which is true: the
model was given exactly the same input twice.

**Session B** lands in `likely_legitimate`, reason: "result changed;
write intervened". The write is real. OpenCode's snapshot recorded the
edit to `invoice.py` in a step that ran only the edit, so that call is
marked as a write (see
[docs/opencode.md](https://github.com/CogentWizards/redundo/blob/main/docs/opencode.md#writes)).

**Session C** delegated the test run to the `general` subagent, which
became its own session linked back to C as a delegation. The model
didn't quite follow orders on the double-check: it dropped the `2>&1`,
so its own run isn't the same call as the subagent's and isn't paired.
What did repeat across the link is the model call after each run, since
both saw the same passing output, and that pair lands in
`cross_task_redundancy`. The subagent's run is also an exact match to
Session B's first run, but those two tasks aren't linked, so it's a
`recurring_pattern`, not a finding.

Building this demo found two real problems:

- OpenCode 1.19 stores each step of a turn as its own assistant
  message, not one message per turn as its schema suggests. Every model
  call in a session got the same similarity fingerprint (the user's
  message) until that was fixed.
- Python writes `__pycache__` when the tests run, and OpenCode's
  snapshot recorded that as a file change, marking the test runs as
  writes. The demo now sets `PYTHONDONTWRITEBYTECODE=1`.

These results were recorded under `similarity_spec` v1 with a 24-bit
near-duplicate threshold. That run also put Session C's call after the
subagent reported back 0 bits from Session B's call after a failing
test, because `unittest`'s long separator lines outvoted the text that
differs. v2 counts each distinct shingle once and lowers the threshold
to 16 (see
[docs/hashing.md](https://github.com/CogentWizards/redundo/blob/main/docs/hashing.md#similarity-fingerprinting)).
Re-adapting the same capture under v2, that pair is 28 bits apart and
no longer reported. The exact-match findings above don't change.
Session B's two `near_duplicate` hits at 20 and 24 bits, which were
noise, are gone, leaving only Session A's 0-bit one. The subagent's
call after its passing run and Session B's call after its passing
re-run saw the same output, so they're now a 0-bit `recurring_pattern`.
The `cross_task_redundancy` pair is no longer reported: it's 9 bits
apart, but no 16-bit band of the two fingerprints matches, and the
cross-task search only compares pairs that share one.
