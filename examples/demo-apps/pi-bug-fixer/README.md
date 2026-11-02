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

## What to look for

- **Session A**'s repeat is the clean "stuck" case. Pi's tool results
  don't say whether a tool changed anything, so write status is unknown
  and, as with every source, the task ending in a normal reply means
  redundo won't call it `confirmed_waste`. See [docs/pi.md](https://github.com/CogentWizards/redundo/blob/main/docs/pi.md).
- **Session B**'s repeat should read as `likely_legitimate`, because
  the result changed.
- **Session C** shouldn't double-count anything from Session B, even
  though its file contains all of Session B's entries. Its re-run of the
  tests repeats Session B's, across two tasks that Pi itself links, so
  it lands in `cross_task_redundancy`, not `recurring_pattern`.
- `redundo drift` shows the chain `demo-b -> demo-c`, the first real
  continuation link redundo has had to work with.
- Every `llm_call` carries Pi's own cost, and every tool event carries
  the model of the call that issued it.
