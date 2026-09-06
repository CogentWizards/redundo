# redundo

Point it at your AI agent's OTLP traces. Get a report on what's wasted.

```
enable telemetry  →  point at the collector output  →  redundo adapt  →  redundo analyze  →  HTML report
```

```bash
redundo adapt --source openinference ./otlp_traces | redundo analyze --format html > report.html
```

`redundo` is two programs joined by a pipe, installed as one package.
**`adapt`** turns OTLP telemetry from a specific agent framework (Hermes,
Claude Code, Claude Cowork, ...) into one common, well-defined event
schema. **`analyze`** reads that schema and runs an actual analysis over
it — today: classifying repeated LLM/tool calls as **confirmed waste**,
**likely legitimate**, or **unclassified**. The schema in between is the
real contract, not the pipe — if your framework isn't supported yet, skip
`adapt` and pipe in your own NDJSON matching [the schema](#the-schema-contract);
`analyze` doesn't know or care where its input came from.

No account, no SaaS, no infrastructure to stand up beyond a local OTLP
receiver. It runs entirely on your machine, over data that never leaves it.

## Quickstart

```bash
pip install "redundo[collector]"

# 1. Start a local OTLP receiver (skip this if you already run a real
#    OTel Collector or backend -- point your source at that instead and
#    use its output directory below).
redundo collect --out-dir ./otlp_traces &

# 2. Enable your agent framework's OTLP export, pointed at
#    http://localhost:4318, and run it. See docs/ for the exact env vars
#    each supported source needs.

# 3. Convert whatever got captured, then analyze it -- one pipe.
redundo adapt ./otlp_traces --summary | redundo analyze --format html > report.html
```

Already have a real collector or backend? Skip step 1 and `pip install
redundo` without the extra — `adapt` and `analyze` themselves have no
dependencies at all; only `collect` needs the extra.

The source (which framework produced the captured data) is detected
automatically — you don't tell it. `--summary` on `adapt` prints what it
found: how many records came out, what fraction have observable content
vs. had to degrade honestly to "unknown," anything it had to skip and why.
See [`docs/`](docs/) for exactly what each source provides and doesn't.

Each half also runs on its own:

```bash
redundo adapt ./otlp_traces -o trace.jsonl      # just convert
redundo analyze trace.jsonl --format json        # just analyze a file
redundo analyze trace.jsonl                       # reads stdin if the path is omitted or "-"
```

## Supported sources

| Source | What it captures | Docs |
|---|---|---|
| **OpenInference** (Hermes, and anything else instrumented with an OpenInference-compatible library) | Full call/result content on both LLM and tool spans | [docs/openinference.md](docs/openinference.md) |
| **Claude Code** (CLI, IDE extensions, Agent SDK) | Tool arguments and output via `OTEL_LOG_TOOL_DETAILS`/`OTEL_LOG_TOOL_CONTENT`; MCP tool arguments require the logs signal | [docs/claude-code.md](docs/claude-code.md) |
| **Claude Cowork** | Logs-signal only; tool *arguments* observable, tool *output* is not, under any configuration | [docs/cowork.md](docs/cowork.md) |
| **OpenClaw** (`@openclaw/diagnostics-otel`) | Content is opt-in (`captureContent`, off by default); `task_id` is always trace-scoped, not conversation-scoped -- see docs/openclaw.md for why that's structural, not a fallback | [docs/openclaw.md](docs/openclaw.md) |

More sources are expected over time — an OpenTelemetry-based agent
observability adapter is only useful if it keeps pace with what people are
actually building agents with. Adding one means adding a module under
`src/redundo/adapter/sources/` and a detection rule in `detect.py`; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## How auto-detection works

Each source has a genuinely different OTLP shape, not just different
attribute names — Claude Code, OpenInference, and OpenClaw all emit real
trace spans (with different span-name/span-kind conventions), while
Cowork emits only the logs signal, with no span tree at all. `redundo
adapt` tells them apart from the data itself: span names,
`openinference.span.kind` attributes, and (for the logs-only case, where
Claude Code and Cowork's event shapes genuinely overlap) the OTLP
resource-level `service.name` attribute. Full detail in
[`detect.py`](src/redundo/adapter/detect.py)'s own docstring. Force a
specific source with `--source` if you ever need to skip detection.

## The schema contract

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
output next. See [`hashing.py`](src/redundo/adapter/hashing.py) for
the exact procedure (masking, canonicalization — it's one file, read it
before trusting it).

### `metadata` conventions

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
  [the hashing doc](docs/hashing.md) for one documented, versioned
  procedure — but if a loaded corpus contains two different `hash_spec`
  values, `load_events()` refuses to proceed rather than silently
  comparing hashes that were computed two different ways. Comparable
  `content_hash` values across sources requires every source to have used
  the same procedure; this is how that gets caught instead of assumed.
- `metadata.task_id_source` (str, optional): names which of a source's
  available signals `task_id` actually came from (e.g.
  `"conversation_id"` vs `"trace_id_fallback"` for OpenInference sources).
  Not every adapter source sets this, and that's fine — see Coverage
  below for what happens when it's absent.
- `metadata.content_basis` (str): which source of content (if any)
  produced `content_hash` — `"prompt"`, `"tool_input"`, `"opaque"`, or
  `"prompt_windowed"`. This is what keeps "nothing repeated" and "nothing
  was observable" from looking identical downstream.

## Coverage: how much of the data this can actually speak to

Every report opens with a coverage line, before any bucket:

```
Coverage: 16/25 events priced (64%) -- $0.1060 of tracked spend is what this analysis actually covers.
  9 event(s) had no cost_usd and are excluded from every dollar figure below -- the percentages are computed on the priced subset, not your total spend.
```

This is measured over the *entire loaded corpus*, not just the events that
ended up in a candidate pair — the point is telling a reader what fraction
of their total data the numbers below are even computed on, before they
trust or forward those numbers. If `metadata.task_id_source` is present on
any event, a second line reports what fraction were grouped by a source's
most precise available signal versus a fallback (see the relevant source
doc under [`docs/`](docs/) for what that fallback costs). If no source in
the loaded corpus ever sets that key, the line is omitted entirely rather
than reporting a fabricated "0%" — silence here means "this dimension
can't be spoken to for this data," not "everything failed."

A third line reports comparability: what fraction of tasks had at least
one candidate pair (a repeated call) for the buckets below to say anything
about, versus tasks where nothing repeated at all and so nothing about
them appears in any bucket. That's not a data gap — every call in those
tasks was simply unique — but without this line, "this task had nothing
to compare" and "this task's spend belongs to a source with missing
signal" both look identical: silent absence from the bucket breakdown.
The line is omitted when every task had at least one candidate pair.

## The three buckets

Given a candidate pair (an original call and a later, identical repeat of
it in the same execution path):

- **confirmed_waste**: identical arguments (that's what makes it a
  candidate pair in the first place), identical result, no intervening
  write, task terminated in failure. All four confirmed, none assumed.
- **likely_legitimate**: result changed, OR a write intervened — either
  one confirmed is enough, unconditionally. Terminal success is also
  legit-supporting, but only as a tie-breaker: if *either* call-level
  signal already confirms waste (identical result, or no intervening
  write — one alone is enough, they needn't agree), the task having
  succeeded anyway doesn't override that. A whole-task outcome can't stand
  in for the missing answer to "did this specific repeat matter" — see
  `classify.py`'s module docstring for the full reasoning, including why
  requiring only one confirmed signal (not both) is what makes a trace
  with no captured result — or one with `result_hash` stripped — degrade
  to unclassified instead of quietly flipping to a false likely_legitimate.
- **unclassified**: everything else. At least one required signal
  (result identity, write status, or terminal outcome) couldn't be read
  off the trace, and no legitimate-use signal fired either — or one
  call-level signal alone confirms the call looks wasted (identical
  result, or no write) and the task merely succeeded anyway, which isn't
  proof the repeat contributed.

The rule itself is printed next to every count in the actual report output,
not left implicit in a label — `"42 confirmed_waste"` is a claim; `"42
confirmed_waste -- repeated call, unchanged result, no intervening write,
task failed"` is a claim someone can check against one case by hand. That's
what makes a report worth forwarding to someone else. See `RULE_TEXT` in
`report.py` for the exact wording, kept in sync with `classify.py`'s actual
decision logic.

Each of the three underlying signals (result, write, terminal outcome) has
three possible readings: waste-supporting, legit-supporting, or unknown.
`confirmed_waste` requires all three positively waste-supporting.
`unclassified` is not a bug to be minimized with heuristics — it's the
honest answer when the trace doesn't say. A confident wrong classification
here is worse than a large unclassified bucket: the first time someone
spot-checks a "confirmed waste" case by hand and finds it wasn't, the tool
stops being trusted. A large unclassified bucket just means the trace
needs richer instrumentation (write flags, response hashes, better
outcome tracking) — which is a legible, actionable gap, not a hidden one.

There's a fourth bucket this analysis deliberately doesn't attempt:
**silent-wrong** (identical call, identical-looking success, wrong answer
both times). That's not computable from a trace alone — it needs a
correctness oracle external to the trace itself. A different analysis
module, built on the same schema, is where something like that would live.

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

## Usage

```bash
redundo analyze trace.jsonl
redundo analyze trace.jsonl --format json
redundo analyze trace.jsonl --format html --output report.html
redundo analyze trace.jsonl --lenient   # skip malformed rows instead of failing
redundo analyze trace.jsonl --analysis waste   # the default -- other analyses can register under this flag
```

The HTML report is a single self-contained file: no CDN assets, no
webfonts, no JS — just inline SVG and CSS, safe to open straight from
disk or send anywhere. Every value pulled from the trace (model names,
workflow labels, classification reasons) is HTML-escaped before being
written, since that content is attacker-controlled if the trace comes
from somewhere untrusted. See `examples/demo_trace.jsonl` /
`examples/demo_report.html` for a worked example covering all three
buckets.

Or as a library:

```python
from redundo.adapter import detect_source, convert_claude_code
from redundo.analyzer import load_events, WasteAnalysis

documents = [...]  # parsed OTLP JSON documents
detection = detect_source(documents)
records, summary = convert_claude_code(documents)  # or convert_openinference / convert_cowork / convert_openclaw

events = load_events("trace.jsonl")
result = WasteAnalysis().run(events)
```

Or run a different analysis the same way -- any `Analysis` subclass takes
a `list[Event]` and returns an `AnalysisResult` that every renderer
(`to_text`/`to_json`/`to_html`) already knows how to display:

```python
from redundo.analyzer import AnalysisRegistry, to_html

result = AnalysisRegistry().get("waste").run(events)
open("report.html", "w").write(to_html(result))
```

Every `Classification` carries a one-line `reason` naming exactly which
signals fired and why — that's what the CLI's "sample cases" section
prints, meant for spot-checking a verdict by hand.

## Design principles

- **The schema is the protocol**, not the pipe. `redundo.adapter` and
  `redundo.analyzer` have no runtime dependency on each other beyond
  agreeing on the schema's shape — that's also why `analyze` reads
  hand-built NDJSON just as happily as `adapt`'s own output.
- **Degrade honestly, never guess.** When a source doesn't provide enough
  information to compute something real (a tool's result content, an
  LLM's response text, whether a call had a side effect), it's either
  omitted or marked explicitly as unobservable — never a fabricated
  placeholder that could be mistaken for real data. `unclassified` is not
  a bug to be minimized with heuristics; it's the honest answer when a
  trace doesn't say. Every source doc in `docs/` has a "known gaps"
  section that says exactly what can't be seen and why.
- **Every non-obvious decision is verified against real captured data**,
  not just a source's published documentation — several of the decisions
  in `docs/claude-code.md` in particular exist specifically because the
  docs and the actual data disagreed.

## Adding a new analysis or a new source

- A new **analysis** over the same schema (cost anomaly detection, latency
  regression, whatever) lives alongside `classify.py`/`cycles.py` as its
  own module, reusing `load_events()`/`Event` and the coverage reporting,
  not replacing them.
- A new **source** adapter is a module under `src/redundo/adapter/sources/`
  plus a detection rule in `detect.py`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the discipline both are held to
(honest coverage reporting, fixture-based validation, never guessing past
a genuine unknown).

## Development

```bash
uv sync
uv run pytest
```

`tests/analyzer/fixtures/sample.jsonl` has one task per bucket (including
each of the three `likely_legitimate` triggers separately) and doubles as
a runnable example. `tests/adapter/` and `tests/analyzer/` run
independently of each other, matching the module split.

## License

MIT — see [LICENSE](LICENSE).
