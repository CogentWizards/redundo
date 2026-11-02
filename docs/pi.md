# Pi

Covers the [Pi coding agent](https://github.com/earendil-works/pi)
(`@earendil-works/pi-coding-agent`, formerly `@mariozechner/pi-coding-agent`).

Pi emits no OpenTelemetry. It doesn't need to: every conversation is
already an append-only JSONL file on disk, with real per-call cost, real
tool-call ids and a real branching tree. So this is the one built-in
source that reads a native capture instead of OTLP. There is nothing to
install or turn on.

```bash
redundo adapt ~/.pi/agent/sessions/--Users-me-src-myproject--/ --summary \
  | redundo analyze --format html > report.html
```

Pass one session file, or one project's session directory. Pi names that
directory after the working directory, with `/` replaced by `-`. The
directory is read non-recursively on purpose: these files hold your full
raw conversation history, so `redundo adapt` only reads what you point it
at, never everything under `~/.pi`. Only hashes leave `adapt`, as with
every other source. Files in the directory that aren't Pi sessions are
ignored.

Pi's own `--session-dir <dir>` (or `PI_CODING_AGENT_SESSION_DIR`) puts a
run's sessions somewhere else, which is the easiest way to analyze one
piece of work on its own.

Format reference: Pi's own
[session-format.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
and [message-types.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/message-types.md).
Session versions 1 to 3 are read. Pi migrates older files to version 3
when it opens them, but files it never reopened stay as written.

## Mapping

| Schema field | From |
|---|---|
| `task_id` | the session header's `id` |
| `llm_call` | each assistant message |
| `tool_call` | each `toolCall` block in an assistant message |
| `tool_result` | each `toolResult` message, joined to its call by `toolCallId` |
| `name` | the assistant message's `model`; the tool's `name` (`namespace.name` for namespaced tools) |
| `cost_usd` | the assistant message's `usage.cost.total`, Pi's own figure |
| `tokens_in` / `tokens_out` | `usage.input + cacheRead + cacheWrite` / `usage.output` |
| `outcome` | `stopReason` `error` or `aborted` is `error`, else `ok`; `isError` on tool results |
| `parent_id` | Pi's own `id`/`parentId` tree |
| `workflow` | always `"main"` (`workflow_basis: "no_delegation"`); Pi has no subagents |
| `model` on tool events | the model of the assistant message that issued the call (`model_basis: "issuing_llm_call"`) |
| `metadata.response_hash` | the assistant message's content, stop reason and error |
| `metadata.parent_task_id` | for a forked or cloned session, its parent session's id, with `parent_task_link_kind: "continuation"` |

## What's hashed

**`llm_call`**: the model-visible context for that call, rebuilt the way
Pi's own `buildSessionContext()` builds it. That's the path from the root
of the tree to the call, with the latest compaction applied (checkpoint
system message, summary, then the entries from `firstKeptEntryId`) and
the latest `context_edit` per target applied (`null` drops the target,
anything else replaces its content). Timestamps, usage, provider
signatures and tool-call ids are dropped first, since they change on
every call without changing what the model was asked. It's close to the
request Pi sent, but not a byte-exact replay: system prompt sections are
listed in order, not merged.

`metadata.similarity_fingerprint` covers only what's new in the request:
the messages after the last assistant reply (the user's turn, or the
tool results the model is responding to). Every context contains the
whole context before it, so fingerprinting the full context would make
every consecutive pair of calls look like a near-duplicate.

<<<<<<< HEAD
=======
Short new inputs, a tool's one-line result for instance, share a lot of
structure, so two unrelated ones can still land under the near-duplicate
threshold. Treat an `llm_call` near-duplicate at a high bit distance as a
weak signal; a 0-bit one means the model really was given the same input
again.

>>>>>>> origin/main
**`tool_call`**: the call's `arguments`. **`tool_result`**: the result's
`content` blocks.

## Things that would otherwise go wrong

**Auto-retries.** When a call fails with a retryable error, Pi keeps the
failed attempt in the file and appends a `context_edit` that hides it
(and its tool results) from later requests. The retry's context therefore
hashes identical to the failed attempt's, and the failed attempt is its
ancestor. That pair is a real repeat. It reads as `likely_legitimate`
because the result changed (error, then success), which is the right
call.

**Forks and clones.** `/fork`, `/clone` and `pi --fork` copy the source
session's entries into the new file with their original ids and
timestamps. Reading those copies as the new session's own work would
report every call before the fork twice, in two tasks. A session whose
header names a `parentSession` skips every entry timestamped before its
own header, which Pi stamps at the fork moment. The copies still count
toward the context of the calls that follow, since the model saw them.

The fork is then linked to its parent with `parent_task_id`, and
`parent_task_link_kind: "continuation"`. Pi is the first source to report
a real continuation link, which is what [`redundo drift`](context-drift.md)
needs. The parent's id comes from the parent session if it was loaded in
the same run, or else from the first line of the parent's file (its
header, nothing else). If neither is available, the fork carries no link,
and `--summary` says so.

**Parallel tool calls.** One assistant message can request several tools
at once. The records chain linearly, in the order Pi recorded the
results: `llm_call`, then each `tool_call` immediately followed by its
own `tool_result`. Every result then stays an ancestor of the next
`llm_call`, which did see all of them. A call that never got a result (an
aborted turn) becomes a `tool_call` right after its `llm_call`, with no
`tool_result`.

**Branches.** Navigating back with `/tree` and continuing creates real
siblings in Pi's tree, and they stay siblings here. Two branches making
the same call is fan-out, never flagged.

## Cost

Pi computes `usage.cost.total` on every assistant message from its own
model registry, and that figure is used as-is (no `cost_basis`, so it
reports as direct from the source).

A total of exactly `0` with nonzero tokens is ambiguous. It's either a
model Pi has no price for, or a local model that really is free. Rather
than report a confident `$0`, that call falls back to the bundled pricing
table (`cost_basis: "estimated_from_bundled_pricing_table"`) and
otherwise stays unpriced. A call with no tokens at all reports `$0`.

Some spend has no call of its own: `usage` entries (cache warming, for
one), the summary generation behind a compaction or `branch_summary`
entry, and nested model work a tool reports on `toolResult.usage`. Each
becomes an `llm_call` record with `metadata.synthesized_cost_only: true`
and `metadata.usage_kind` naming which (`cache_warm`, `compaction`,
`branch_summary`, `tool_nested`, or whatever `kind` a `usage` entry
carries). These count toward dollars and are never repeat candidates.

## Not available

- **Write flag.** Pi's tool results don't say whether a tool changed
  anything, and this package never infers a side effect from a tool's
  name, even `write` or `edit`. Write status reads as unknown, so a
  repeat whose result and outcome are otherwise conclusive can still land
  in `unclassified`.
- **Codemode's nested calls.** A `codemode` result lists the tools its
  script called in `nestedCalls`, with arguments but without results.
  They aren't emitted as calls of their own.
- **Extensions' subagents.** An extension that runs its own agents
  records them however it likes. Only the main conversation's messages
  are read.
