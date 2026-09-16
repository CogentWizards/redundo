# Demo: a daily-briefing assistant, built on OpenClaw

A real, minimal "give me today's briefing" agent, run against a real,
local OpenClaw Gateway with our own [openclaw-localtrace
plugin](https://github.com/CogentWizards/openclaw-localtrace) capturing
telemetry. No mocking: two real conversations, real `web_search` and
`write_file` tool calls, run straight through `redundo adapt | redundo
analyze`.

```bash
./run.sh
```

Needs `openclaw` on your `PATH`, already configured with a working model
provider and at least one agent (see `openclaw agents list`; override
which one this uses with `OPENCLAW_DEMO_AGENT=<id> ./run.sh`), and the
`openclaw-localtrace` plugin already installed and enabled per its own
README (`hooks.allowConversationAccess`, `config.captureContent`,
`config.captureIdentifiers` all `true`). Takes a minute or two.

## What it does

**Session A** asks for a quick tech-news search, gets back a generic,
unhelpful result (real DuckDuckGo search behavior -- freshness-filtered
queries often surface section-front pages, not individual headlines),
and re-runs the *exact same* search once more, verbatim, before giving
up. A repeated, identical tool call with nothing written in between --
about as clean a "was that worth doing twice" case as this domain gets.

**Session B** searches, saves the result to `briefing.md` (a real write
-- `write_file` is one of `openclaw-localtrace`'s default
`mutatingToolNames`), then re-runs the *exact same* search again to
double-check before sending. Two more searches, each rephrased to look
at the story from a different angle, round it out.

## What actually happens on real data, and why

**Session A** lands in `unclassified`, reason: "result identical; no
intervening write; task succeeded, but that doesn't confirm this
specific repeated call contributed." A human skimming the transcript
would call the repeat pointless without a second thought -- the search
really was byte-for-byte identical both times (the agent's own reply
says as much: "down to the `tookMs` and `cached` flag"), and redundo now
correctly sees that (see the wrapper-id masking note below). What it
won't do is borrow "a human would call this waste" to fill the one gap
that's left: the session's last recorded event is the model's own
closing reply, which succeeds as a model call regardless of what it
says, so the trace's own terminal-outcome signal reads "success." That's
the same honest limitation the `claude-agent-sdk-code-review` demo's
Session A hits, on a completely different product -- seeing the same
shape of "can't confirm this specific call mattered" independently on a
second, unrelated source is itself a useful signal, not a demo bug.

**Session B** lands in `likely_legitimate` -- correctly, and for the
intended reason: the write (`briefing.md`) really did intervene before
the recheck, and this session doesn't need the search result itself to
have changed for that verdict to be right.

Getting Session A's identical repeat to actually read as identical took
two real fixes, both found and fixed while building this demo: OpenClaw
wraps every piece of external tool content in a
`<<<EXTERNAL_UNTRUSTED_CONTENT id="...">>>` marker (its own
prompt-injection defense) with a fresh random id on every single call,
even when the wrapped content is identical, and redundo's masking didn't
catch it -- first because the id wasn't masked at all
([#30](https://github.com/CogentWizards/redundo/pull/30)), then because
the mask's own lookbehind/lookahead assumed a bare `"` around the id,
which isn't what the real, doubly-JSON-encoded payload actually contains
([#33](https://github.com/CogentWizards/redundo/pull/33), found by
re-verifying this exact demo after the first fix landed).

The two rephrased searches in Session B land in `near_duplicate` as
intended; the two sessions' matching opening searches (and closely
related model turns) land in `recurring_pattern`, since nothing links
these two independent conversations.
