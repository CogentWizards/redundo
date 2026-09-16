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

Both sessions' repeated searches land in `likely_legitimate`, reason
"result changed" -- for Session B that's correct and expected (the
write really did intervene, and this demo doesn't need the result to
have changed for that verdict to be right). For **Session A it's a real,
interesting false-negative-adjacent finding**, not the intended
`confirmed_waste`/`unclassified` split this demo set out to show: the
underlying search result was byte-for-byte the same both times (the
agent's own reply says as much -- "down to the `tookMs` and `cached`
flag"), but `content_hash` still came out different, because OpenClaw
wraps every piece of external tool content in a
`<<<EXTERNAL_UNTRUSTED_CONTENT id="...">>>` marker (its own
prompt-injection defense) with a **fresh random id on every single
call**, even when the wrapped content is identical. redundo's masking
doesn't yet strip these ids (they're not UUID-shaped, so the existing
UUID mask doesn't catch them), so two genuinely identical search results
still hash differently. See the follow-up task tracking a proper fix
(mask the wrapper's id before hashing, the same way volatile timestamps
and temp paths already are).

This is exactly the kind of thing running these demos against real data
exists to surface: not a demo bug, and not a bug in the intended
scenario either, but a real gap in how redundo processes this
particular source, found by actually looking at what the trace
contained rather than assuming the design would land as planned.

The two rephrased searches in Session B land in `near_duplicate` as
intended; the two sessions' matching opening searches (and closely
related model turns) land in `recurring_pattern`, since nothing links
these two independent conversations.
