# OpenCode

Covers [OpenCode](https://github.com/anomalyco/opencode) 1.x (the
`opencode-ai` package, formerly `sst/opencode`).

OpenCode has no usable OpenTelemetry export: its experimental
`openTelemetry` flag only ever produced AI SDK spans, and OpenCode 2.x
removes it. But every session is already stored locally with real
per-call cost, tool inputs and outputs, and subagent links, so `redundo
adapt` reads that directly. There's nothing to install or turn on.

Two ways in, with the same result:

```bash
# One session (the scoped option): export it, then adapt the file.
opencode export ses_XXXXXXXX > session.json
redundo adapt session.json --summary | redundo analyze --format html > report.html

# Every session OpenCode has stored, straight from its database.
redundo adapt "$(opencode db path)" --summary | redundo analyze --format html > report.html
```

`opencode session list` shows session ids. A directory of exported
`.json` files works too, one session per file. The database holds every
session from every project on the machine, so exporting the sessions you
care about is the narrower choice. Either way only hashes leave `adapt`.
The database is opened read-only, so it's safe to run while OpenCode is
open.

Export without `--sanitize`. A sanitized export replaces content with a
different `[redacted:...]` token for every part, so two identical calls
can't be told apart. Those records are hashed as opaque (they never
match anything, and never read as "the result changed"), and `--summary`
says how many there were.

Format reference: OpenCode's `packages/schema/src/v1/session.ts` and
`packages/core/src/session/sql.ts`, read at the `v1.19.5` tag. OpenCode
2.x keeps sessions in different tables (`session_v2`, `session_message`,
`session_part`). A 2.x-only database is refused with an error that says
so, rather than read halfway.

## Mapping

| Schema field | From |
|---|---|
| `task_id` | the session id (`ses_...`) |
| `llm_call` | each step of an assistant message (`step-start` to `step-finish`) |
| `tool_call` / `tool_result` | each `tool` part: `state.input`, then `state.output` (completed) or `state.error` (error) |
| `name` | the message's `modelID`; the tool's name |
| `cost_usd` | the step's own `step-finish.cost`, OpenCode's figure |
| `tokens_in` / `tokens_out` | `input + cache.read + cache.write` / `output + reasoning` |
| `outcome` | a step with a `step-finish` is `ok`, one without is `error`; a tool's `status` |
| `parent_id` | the previous record (OpenCode sessions are linear) |
| `workflow` | the message's `agent`: `build`, `plan`, a subagent's name, `compaction` (`workflow_basis: "agent"`) |
| `model` on tool events | the model of the step that issued the call (`model_basis: "issuing_llm_call"`) |
| `metadata.parent_task_id` | a subagent session's `parentID`, with `parent_task_link_kind: "delegation"` |
| `metadata.write` | see below |

**Why steps, not messages.** One assistant message in OpenCode is a whole
turn: the model calls a tool, gets the result, and goes again, all inside
the same message. Each of those rounds is a step with its own
`step-finish` part carrying that call's cost and tokens. The message's
own cost is the sum of its steps, so it's only used for a message that
failed before its first step began.

**Tool calls without a result.** A tool still `pending` or `running` when
the turn stopped becomes a `tool_call` with no `tool_result`.

**Retries.** OpenCode retries a failed API request inside the same step
and records a `retry` part. A failed attempt reports no usage, so it
isn't a record of its own; `--summary` counts them.

## What's hashed

**`llm_call`**: the model-visible context for that step, rebuilt the way
OpenCode's own `filterCompacted()` builds it. That's every message before
it, cut at the latest completed compaction (the compaction request, its
summary, the retained tail from `tail_start_id`, then everything after),
plus this message's earlier steps. Ids, timestamps, snapshots and
per-part metadata are dropped first, since they change on every call
without changing what the model was asked. It's close to what OpenCode
sent, but not a byte-exact replay: the system prompt OpenCode assembles
at request time isn't stored, so it isn't included.

`metadata.similarity_fingerprint` covers only what's new for that step:
the user's message for a turn's first step, or the previous step's tool
calls and results. Every context contains the one before it, so a
whole-context fingerprint would flag every consecutive pair of calls as
near-duplicates.

OpenCode 1.19 writes each step of a turn as its own assistant message.
That's handled: a message's first step takes the previous message's last
step as its new input, not the user's message.

<<<<<<< HEAD
=======
Short new inputs that are mostly separator lines (Python's `unittest`
prints 70 `=` or `-` characters per line, for instance) can fingerprint
close together even when their text differs, because those repeated
characters outvote the rest. Treat a near-duplicate between two test runs
with a grain of salt; `content_hash` equality is what proves a repeat.

>>>>>>> origin/main
**`tool_call`**: `state.input`. **`tool_result`**: `state.output`, or
`state.error` for a failed call.

## Writes

OpenCode snapshots the working tree around each step, and when files
changed it adds a `patch` part listing them. That's a real record that
the step changed something. When a step with a patch ran exactly one
tool, that tool call is marked `metadata.write: true`
(`write_basis: "step_patch_single_tool"`), since nothing else in the step
could have made the change. When it ran several, which one wrote is
unknown, so none is marked.

The snapshot counts any file that changed, build output included. A test
run that writes `__pycache__` or a `dist/` folder the project doesn't
gitignore gets a patch too, and its tool call is marked as a write. That's
true to what happened on disk, but it isn't the kind of write that makes
a repeat legitimate. Gitignore build output to keep it out.

A step with no patch part isn't marked as a non-write. Snapshots only
see files git tracks in the project, they can be turned off, and a tool
can have side effects outside the working tree. As everywhere in redundo,
nothing is inferred from a tool's name.

## Cost

`step-finish.cost` is OpenCode's own figure, from its models.dev pricing,
used as-is. A cost of exactly `0` with nonzero tokens is ambiguous: a
model OpenCode has no price for, or one that's genuinely free. Rather than report a confident `$0`, that call
falls back to the bundled pricing table
(`cost_basis: "estimated_from_bundled_pricing_table"`) and otherwise stays
unpriced.

## Not available

- **The system prompt.** It isn't stored, so it isn't part of the context
  hash. Two calls that differ only in system prompt hash the same.
- **OpenCode 2.x.** Its session tables have a different shape. Supporting
  them is a separate piece of work, once 2.x is a stable release.
- **The legacy JSON tree.** OpenCode before 1.2 stored sessions as JSON
  files under `~/.local/share/opencode/storage/`. OpenCode migrates those
  into the database, so read the database instead.
