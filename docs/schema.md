# The schema contract

One row per event, one JSON object per line (NDJSON). This is the actual
interface between `adapt` and `analyze`. Either half can be swapped or
reimplemented independently as long as it agrees on this shape.

| field | meaning |
|---|---|
| `task_id` | session/conversation/trace grouping key |
| `step_index` | ordering within task |
| `event_type` | `llm_call` \| `tool_call` \| `tool_result` |
| `name` | model or tool name |
| `content_hash` | hash of prompt or arguments, never raw content |
| `tokens_in` / `tokens_out` | if the source has them |
| `outcome` | `ok` \| `error` \| empty |
| `timestamp` | when the event happened |
| `cost_usd` | dollar-denominated cost, if the source has it directly, or an adapter's own estimate/apportionment when it doesn't (see `metadata.cost_basis` below and [docs/pricing.md](pricing.md)) |
| `model` | cost fallback, and waste segmented by model |
| `parent_id` | the `step_index` (within this `task_id`) of the event that produced/spawned this one |
| `workflow` | free-text segmentation label (agent name, pipeline stage, ...) |
| `metadata` | escape hatch: anything else, keyed by convention (below) |

`task_id` + `step_index` is an event's identity. `parent_id` points at
another event's `step_index` within the same task.

**Nothing downstream of `adapt` ever sees raw prompt or tool content,
only hashes.** That's deliberate: it's what makes it safe to run this
over production traces without a security review of whatever reads the
output next. See [`hashing.py`](../src/redundo/adapter/hashing.py) for
the exact procedure (masking, canonicalization). It's one file, read it
before trusting it.

## `metadata` conventions

Keys `analyze` looks for. Absence is not a claim of a default value.

- `metadata.write` (bool): does this **call** have a side effect? Only
  checked on call-type events, never on `tool_result` rows, since a result
  is an outcome, not an action, and can't independently mutate anything.
  For `tool_call`, if a source never sets this, write status reads as
  unknown, not "no write": a tool can plausibly do anything. For
  `llm_call`, an unset flag defaults to "no write" instead. The event type
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
  This package has no opinion on what that procedure should be. See
  [hashing.md](hashing.md) for one documented, versioned procedure. But
  if a loaded corpus contains two different `hash_spec` values,
  `load_events()` refuses to proceed rather than silently comparing
  hashes that were computed two different ways. Comparable `content_hash`
  values across sources requires every source to have used the same
  procedure; this is how that gets caught instead of assumed.
- `metadata.task_id_source` (str, optional): names which of a source's
  available signals `task_id` actually came from (e.g.
  `"conversation_id"` vs `"trace_id_fallback"` for OpenInference sources).
  Not every adapter source sets this, and that's fine. See the README's
  Coverage section for what happens when it's absent.
- `metadata.content_basis` (str): which source of content (if any)
  produced `content_hash`: `"prompt"`, `"tool_input"`, `"opaque"`, or
  `"prompt_windowed"`. This is what keeps "nothing repeated" and "nothing
  was observable" from looking identical downstream.
- `metadata.similarity_fingerprint` (str): a SimHash fingerprint of the
  call's own prompt/arguments (never its result), used only by the
  `near_duplicate` bucket to find similar-but-not-identical repeats.
  Absent means the source doesn't populate it. It's never treated as
  "definitely no near-duplicate."
- `metadata.similarity_spec` (str, optional but recommended): names the
  version of the fingerprint procedure used to compute
  `similarity_fingerprint`, the same "don't silently compare
  incompatible hashes" discipline `hash_spec` applies to `content_hash`.
- `metadata.cost_basis` (str, optional): which real signal produced this
  event's `cost_usd`, when it isn't the source's own directly-reported
  figure. Absent means direct: the source reported `cost_usd` itself,
  as-is. Known values in use today: `"estimated_from_bundled_pricing_table"`
  (an adapter's own estimate from token counts against a bundled
  pricing snapshot, see [docs/pricing.md](pricing.md)) and
  `"apportioned_from_metrics_by_tokens"` / `"apportioned_from_metrics_equal_split"`
  (a share of an aggregate metrics counter split across the calls it
  covers, see [docs/openclaw.md](openclaw.md)). A record with
  `metadata.synthesized_cost_only` set is reported under a further
  distinct basis regardless of this key. `redundo analyze`'s own report
  states the dollar mixture across these, so a real billed amount and a
  bundled-price-table guess never look equally authoritative.

## Design decisions

The schema names `parent_id` but doesn't say what to do when a source
doesn't populate it, or how "redundant repeat" should behave in branched
traces. These are the choices this implementation makes, made explicit so
they're checkable:

1. **No `parent_id`, no branching.** If a source never sets `parent_id`,
   every event's effective parent is simply the immediately preceding
   event (by `step_index`) in the same task: one linear thread. This
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
   only, not every ancestor, and not the farthest one. This isn't just
   to avoid double-counting: pairing against the nearest match is what
   makes "did anything change since the last time this exact call
   happened" answerable correctly. See `cycles.py`'s module docstring for
   the worked example of why pairing against a farther match instead can
   make a genuinely wasteful repeat look legitimate.
4. **Terminal outcome is task-level, not branch-precise.** "The task
   terminated in failure/success" is read from the last event (by
   `step_index`) in the whole `task_id`, not the specific branch a
   candidate pair sits in. For genuinely parallel/multi-branch tasks with
   independently-resolving branches this is an approximation, a
   documented limitation, not a silent one.
5. **Every dollar states its own origin, never presented as equally
   authoritative.** `cost_usd` is used directly when a source provides
   it. When it doesn't, some adapters estimate it from real per-call
   token counts against a bundled, LiteLLM-derived pricing table
   (`redundo update-pricing` refreshes it, see [docs/pricing.md](pricing.md)),
   or apportion a share of an aggregate metrics counter across the calls
   it covers (see [docs/openclaw.md](openclaw.md)). Either way, the
   adapter sets `metadata.cost_basis` explicitly, never silently, and a
   report's coverage line breaks "tracked spend" down by basis, so a
   real billed amount and a bundled-price-table guess are never mixed
   into one number that looks equally authoritative. When a record has
   no cost signal at all, the slice's dollar total stays at zero, the
   count of unpriced repeats is surfaced explicitly, and token totals
   plus a per-model breakdown are reported instead, so a price can still
   be applied downstream without this package guessing.

## The six buckets

Given a candidate pair (an original call and a later, identical repeat of
it in the same execution path):

- **confirmed_waste**: identical arguments (that's what makes it a
  candidate pair in the first place), identical result, no intervening
  write, task terminated in failure. All four confirmed, none assumed.
- **likely_legitimate**: result changed, or a write intervened. Either
  one confirmed is enough, unconditionally. Terminal success is also
  legit-supporting, but only as a tie-breaker: if *either* call-level
  signal already confirms waste (identical result, or no intervening
  write; one alone is enough, they needn't agree), the task having
  succeeded anyway doesn't override that. See `classify.py`'s module
  docstring for the full reasoning.
- **unclassified**: everything else. At least one required signal
  (result identity, write status, or terminal outcome) couldn't be read
  off the trace, and no legitimate-use signal fired either, or one
  call-level signal alone confirms the call looks wasted and the task
  merely succeeded anyway, which isn't proof the repeat contributed.
- **near_duplicate**: a lower-confidence, differently-shaped finding.
  Arguments *similar but not identical* to an earlier call on the same
  path (a SimHash fingerprint comparison, not exact `content_hash`
  equality). Deliberately not folded into the three verdicts above: this
  is a similarity claim, not a waste/legitimate outcome, and stating it
  as a bucket of its own keeps that distinction visible instead of
  overstating what a fingerprint comparison can support. Never
  double-counted against an exact match already in one of the three
  buckets above. See [hashing.md](hashing.md) for what a similarity
  fingerprint can and can't support.
- **cross_task_redundancy**: a same-or-similar call as an earlier one in
  a *different* task, where the two tasks are confirmed related, a
  real, source-reported delegation link connects them (see
  [openinference.md](openinference.md)'s `parent_task_id` section),
  never inferred from timing or content. Surfaced for review, not a
  waste verdict: what changed between the two calls isn't checked yet,
  that needs cross-task write/outcome semantics this analysis doesn't
  have.
- **recurring_pattern**: a same-or-similar call recurring across tasks
  with *no* confirmed relationship to each other, pure content
  coincidence at corpus scale. Never a waste or legitimate claim, and not
  evidence the two tasks are related, a frequency observation, most
  likely a common or generic operation. Kept in its own bucket
  specifically so it's never misread as the same kind of finding as the
  other five.

The last two search the *whole* corpus, not one task's own execution
path, which is what makes them able to catch redundant work spanning
separate runs of the same long-lived workflow, or separate agents
delegated to from the same one, neither of which the first four buckets
can see at all (they're scoped to a single task by design).

The rule itself is printed next to every count in the actual report
output, not left implicit in a label. `"42 confirmed_waste"` is a claim;
`"42 confirmed_waste: repeated call, unchanged result, no intervening
write, task failed"` is a claim someone can check against one case by
hand. `unclassified` is not minimized with heuristics: a confident wrong
classification is worse than a large unclassified bucket, because the
first time someone spot-checks a "confirmed waste" case by hand and finds
it wasn't, the tool stops being trusted.

There's a further bucket this analysis deliberately doesn't attempt:
**silent-wrong** (identical call, identical-looking success, wrong answer
both times). That's not computable from a trace alone. It needs a
correctness oracle external to the trace itself. A different analysis
module, built on the same schema, is where something like that would
live.

## Why trust these numbers

Every report opens with a coverage line, before any bucket:

```
Coverage: 16/25 events priced (64%). $0.1060 of tracked spend is what this analysis actually covers.
  9 event(s) had no cost_usd and are excluded from every dollar figure below. Percentages are computed on the priced subset, not your total spend.
  Cost basis: $0.1060 (100%) reported directly by the source.
```

This is measured over the *entire loaded corpus*, not just the events
that ended up in a candidate pair. The point is telling a reader what
fraction of their total data the numbers below are even computed on,
before they trust or forward those numbers. The cost basis line (see
`metadata.cost_basis` above) states, for the priced subset, what
fraction of those dollars were reported directly by the source versus
estimated, apportioned, or synthesized by an adapter, omitted only when
nothing is priced at all. If `metadata.task_id_source` is present on any
event, a further line reports what fraction were grouped by a source's
most precise available signal versus a fallback. If no source in the
loaded corpus ever sets that key, the line is omitted entirely rather
than reporting a fabricated "0%": silence here means "this dimension
can't be spoken to for this data," not "everything failed."

Every bucket's own dollar figure is also projected at an assumed call
volume (1,000 calls/day by default, `--calls-per-day` to change it):
"3 confirmed_waste ... at 1,000 calls/day: ~$52.50/mo projected". A
sample trace is often a handful of calls captured during development,
so the raw figure alone reads as too small to matter even when the
underlying repeat pattern is real. The projection is always labeled a
hypothetical, never a measurement: it scales this bucket's own
cost-per-call ratio in the loaded sample to the assumed volume, which
real traffic composition may not match.

A third line reports comparability: what fraction of tasks had at least
one candidate pair (a repeated call) for the buckets below to say
anything about, versus tasks where nothing repeated at all and so
nothing about them appears in any bucket. That's not a data gap (every
call in those tasks was simply unique), but without this line, "this
task had nothing to compare" and "this task's spend belongs to a source
with missing signal" both look identical: silent absence from the bucket
breakdown.

Two things hold across every source and every analysis in this repo:

- **Degrade honestly, never guess.** When a source doesn't provide enough
  information to compute something real (a tool's result content, an
  LLM's response text, whether a call had a side effect), it's either
  omitted or marked explicitly as unobservable, never a fabricated
  placeholder that could be mistaken for real data. `unclassified` is not
  a bug to be minimized with heuristics; it's the honest answer when a
  trace doesn't say. Every source doc in `docs/` has a "known gaps"
  section that says exactly what can't be seen and why.
- **Every non-obvious decision is verified against real captured data**,
  not just a source's published documentation. Several of the decisions
  in `docs/claude-code.md` in particular exist specifically because the
  docs and the actual data disagreed.
