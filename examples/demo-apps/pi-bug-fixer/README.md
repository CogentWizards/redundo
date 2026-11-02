# Demo: a bug fixer, built on Pi

Three real [Pi](https://github.com/earendil-works/pi) sessions against a
tiny invoicing repo with one real bug and one test that can't pass from
inside the repo. No collector and no instrumentation: Pi already writes
every conversation to a session file, and `redundo adapt` reads those
directly.

```bash
./run.sh
```

Needs `pi` on your `PATH` (`npm install -g @earendil-works/pi-coding-agent`),
already set up with a working model provider. Pick the model with
`PI_DEMO_MODEL=<pattern> ./run.sh`; unset, Pi uses your configured
default. The sessions go to a throwaway `--session-dir`, never your real
`~/.pi`. Takes a minute or two.

## What it does

**Session A** runs a test that reads a fixture file from outside the
repo, which doesn't exist, so it can't pass. It's told to re-run the
exact same command once to rule out flakiness, then stop without
touching anything. An identical repeated call with an identical failing
result.

**Session B** runs the line-total tests, fixes the real bug in
`invoice.py` (the discount is applied as a fraction when it's a
percentage), then re-runs the same command to confirm. The same call
twice, but the result changes in between.

**Session C** is forked from Session B with `pi --fork`. Pi copies all
of Session B's history into the new session file, then Session C
re-runs the tests once more and adds a docstring example.

## What actually happens on real data

Verified against a real run (Pi 1.5.0, `claude-opus-4-8`, `hash_spec`
v2, `similarity_spec` v3, 16-bit near-duplicate threshold): 11 model
calls, 8 tool calls, $0.0744, every model call priced by Pi itself.

**Session A** lands in `unclassified`, reason: "result identical; no
intervening write; task succeeded, but that doesn't confirm this
specific repeated call contributed." The repeat really was identical,
down to the failing output. Pi's tool results don't say whether a tool
changed anything, so write status is unknown, and the task ended in a
normal reply, so redundo won't promote it to `confirmed_waste`. That's
the same honest limit the Claude Agent SDK and OpenClaw demos hit, see
[docs/pi.md](https://github.com/CogentWizards/redundo/blob/main/docs/pi.md).
Its second model call got exactly the same input as its first (the same
failing test output), so the two are a 0-bit `near_duplicate`. That's
the only `near_duplicate` in the run.

**Session B** lands in `likely_legitimate`, reason: "result changed".
The tests failed before the edit and passed after it.

**Session C** is where Pi's fork handling shows. Its file holds all 13
of Session B's entries, copied verbatim, and `--summary` reports "13
copied entries skipped", so none of Session B's calls are counted
twice. Pi itself links the fork to Session B, so its repeats land in
`cross_task_redundancy`, three of them:

- its test run, an exact match to Session B's first one
- its model call after that run, 0 bits from Session B's call after its
  passing re-run, since both saw the same passing output
- its model call after its edit, 0 bits from Session B's call after its
  edit, since both edits returned the same one-line result

`redundo drift` shows the chain `demo-b -> demo-c`, the first real
continuation link redundo has had to work with.

Session A's and Session B's first test commands differ only in the test
class name, 8 bits apart. Those sessions aren't linked, so that pair is
a `recurring_pattern`, which claims nothing.

Building this demo found two real problems, both fixed before it
shipped: Pi stamps an undocumented `durationMs` on every message, which
had to be kept out of the context hash, and the report called any
cross-task pair with identical fingerprints an "exact match", even when
the content differed. An earlier run also showed `unittest`'s separator
lines pulling unrelated fingerprints together, which `similarity_spec`
v2 fixed (see
[docs/hashing.md](https://github.com/CogentWizards/redundo/blob/main/docs/hashing.md#similarity-fingerprinting)).
