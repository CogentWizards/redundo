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

Verified against a real run (Pi 1.5.0, `claude-opus-4-8`): 11 model calls,
8 tool calls, $0.0745, every model call priced by Pi itself.

**Session A** lands in `unclassified`, reason: "result identical; no
intervening write; task succeeded, but that doesn't confirm this
specific repeated call contributed." The repeat really was identical,
down to the failing output. Pi's tool results don't say whether a tool
changed anything, so write status is unknown, and the task ended in a
normal reply, so redundo won't promote it to `confirmed_waste`. That's
the same honest limit the Claude Agent SDK and OpenClaw demos hit, see
[docs/pi.md](https://github.com/CogentWizards/redundo/blob/main/docs/pi.md).

**Session B** lands in `likely_legitimate`, reason: "result changed".
The tests failed before the edit and passed after it.

**Session C** is where Pi's fork handling shows. Its file holds all 13
of Session B's entries, copied verbatim, and `--summary` reports "13
copied entries skipped", so none of Session B's calls are counted
twice. Its re-run of the tests lands in `cross_task_redundancy` (an
exact match to Session B's first test run), because Pi itself links the
fork to Session B. `redundo drift` shows the chain `demo-b -> demo-c`,
the first real continuation link redundo has had to work with.

Two `near_duplicate` hits are worth reading closely. Session A's second
model call got exactly the same input as its first (the same failing
test output), a 0-bit match, which is true and is the point. Session B's
call after its edit and its call after reading the file sit 21 bits
apart, under the 24-bit threshold, because short tool outputs share a
lot of structure. That one is noise.

These results were recorded under `similarity_spec` v1 with a 24-bit
near-duplicate threshold. v2 counts each distinct shingle once and
lowers the threshold to 16 (see
[docs/hashing.md](https://github.com/CogentWizards/redundo/blob/main/docs/hashing.md#similarity-fingerprinting)).
Re-adapting the same sessions under v2 leaves the exact-match findings
unchanged. The 21-bit noise hit in Session B is gone, leaving only
Session A's 0-bit `near_duplicate`. `cross_task_redundancy` gains a
third pair: Session C's call after its edit and Session B's call after
its edit both saw the same one-line edit result, so they're 0 bits
apart. Session A's and Session B's first test commands, 6 bits apart
under v1, are 8 bits apart under v2. No 16-bit band of the two
fingerprints matches, and the cross-task search only compares pairs
that share one, so that `recurring_pattern` hit is gone.

Building this demo found two real problems, both fixed before it
shipped: Pi stamps an undocumented `durationMs` on every message, which
had to be kept out of the context hash, and the report called any
cross-task pair with identical fingerprints an "exact match", even when
the content differed.
