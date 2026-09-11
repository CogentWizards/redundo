# The schema contract

One row per event, one JSON object per line (NDJSON). This is the actual
interface between `adapt` and `analyze` — either half can be swapped or
reimplemented independently as long as it agrees on this shape.

| field | meaning |
|---|---|
| `task_id` | session/conversation/trace grouping key |
| `step_index` | ordering within task |
| `event_type` | `llm_call` \| `tool_call` \| `tool_result` |
| `name` | model or tool name |
| `content_hash` | hash of prompt or arguments — never raw content |
| `tokens_in` / `tokens_out` | if the source has them |
| `outcome` | `ok` \| `error` \| empty |
| `timestamp` | when the event happened |
| `cost_usd` | dollar-denominated cost, if the source has it directly (no adapter ever computes cost from a price table) |
| `model` | cost fallback, and waste segmented by model |
| `parent_id` | the `step_index` (within this `task_id`) of the event that produced/spawned this one |
| `workflow` | free-text segmentation label (agent name, pipeline stage, ...) |
| `metadata` | escape hatch: anything else, keyed by convention (below) |

`task_id` + `step_index` is an event's identity; `parent_id` points at
another event's `step_index` within the same task.

**Nothing downstream of `adapt` ever sees raw prompt or tool content —
only hashes.** That's deliberate: it's what makes it safe to run this
over production traces without a security review of whatever reads the
output next. See [`hashing.py`](../src/redundo/adapter/hashing.py) for
the exact procedure (masking, canonicalization — it's one file, read it
before trusting it).

## `metadata` conventions

Keys `analyze` looks for. Absence is not a claim of a default value.

- `metadata.write` (bool): does this **call** have a side effect? Only
  checked on call-type events, never on `tool_result` rows, since a result
  is an outcome, not an action, and can't independently mutate anything.
  For `tool_call`, if a source never sets this, write status reads as
  unknown, not "no write" — a tool can plausibly do anything. For
  `llm_call`, an unset flag defaults to "no write" instead: the event type
  itself is a model completion, not an action, so absence isn't ambiguous
  the way it is for a tool call. A source can still override this by
  setting `write: true` explicitly on an `llm_call` (e.g. embedded
  function-calling that mutates state directly); the default only fills
  in when the field is unset.
- `metadata.response_hash` (str): hash of an `llm_call`'s completion. There
  is no `llm_result` event type in this schema, so an LLM call's output is
  otherwise unobservable. Without this, two identical prompts can never be
  confirmed to have produced identical results, only unknown.
- `metadata.hash_spec` (str, optional but recommended): names the exact
  normalization/masking procedure a source used to compute `content_hash`.
  This package has no opinion on what that procedure should be — see
  [hashing.md](hashing.md) for one documented, versioned procedure — but
  if a loaded corpus contains two different `hash_spec` values,
  `load_events()` refuses to proceed rather than silently comparing
  hashes that were computed two different ways. Comparable `content_hash`
  values across sources requires every source to have used the same
  procedure; this is how that gets caught instead of assumed.
- `metadata.task_id_source` (str, optional): names which of a source's
  available signals `task_id` actually came from (e.g.
  `"conversation_id"` vs `"trace_id_fallback"` for OpenInference sources).
  Not every adapter source sets this, and that's fine — see the README's
  Coverage section for what happens when it's absent.
- `metadata.content_basis` (str): which source of content (if any)
  produced `content_hash` — `"prompt"`, `"tool_input"`, `"opaque"`, or
  `"prompt_windowed"`. This is what keeps "nothing repeated" and "nothing
  was observable" from looking identical downstream.
- `metadata.similarity_fingerprint` (str): a SimHash fingerprint of the
  call's own prompt/arguments (never its result), used only by the
  `near_duplicate` bucket to find similar-but-not-identical repeats.
  Absent means the source doesn't populate it — never treated as
  "definitely no near-duplicate."
- `metadata.similarity_spec` (str, optional but recommended): names the
  version of the fingerprint procedure used to compute
  `similarity_fingerprint`, the same "don't silently compare
  incompatible hashes" discipline `hash_spec` applies to `content_hash`.

## Design decisions where the contract was underspecified

The schema names `parent_id` but doesn't say what to do when a source
doesn't populate it, or how "redundant repeat" should behave in branched
traces. These are the choices this implementation makes, made explicit so
they're checkable:

1. **No `parent_id`, no branching.** If a source never sets `parent_id`,
   every event's effective parent is simply the immediately preceding
   event (by `step_index`) in the same task — one linear thread. This
   makes the tool useful on flat traces (most harnesses produce these)
   without requiring branch instrumentation up front. When `parent_id` is
   populated, it wins.
2. **"Redundant repeat" is lineage-relative.** Two identical calls are a
   candidate pair only if one is an ancestor of the other along the
   parent chain. Two sibling branches making the same call independently
   is normal fan-out, not waste, and is never flagged.
3. **Each event is "the repeat" at most once, against its nearest match.**
   A chain of N identical calls (A -> B -> C -> ...) is N-1 candidate
   pairs, each event compared against its *nearest* matching ancestor
   only — not every ancestor, and not the farthest one. This isn't just
   to avoid double-counting: pairing against the nearest match is what
   makes "did anything change since the last time this exact call
   happened" answerable correctly. See `cycles.py`'s module docstring for
   the worked example of why pairing against a farther match instead can
   make a genuinely wasteful repeat look legitimate.
4. **Terminal outcome is task-level, not branch-precise.** "The task
   terminated in failure/success" is read from the last event (by
   `step_index`) in the whole `task_id`, not the specific branch a
   candidate pair sits in. For genuinely parallel/multi-branch tasks with
   independently-resolving branches this is an approximation — a
   documented limitation, not a silent one.
5. **No price table.** `cost_usd` is used directly when a source provides
   it. When it doesn't, dollar totals for that slice stay at zero, the
   count of unpriced repeats is surfaced explicitly, and token totals plus
   a per-model breakdown are reported instead — so a price can be applied
   downstream without this package guessing or going stale.
