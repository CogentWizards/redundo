# redundo

Point it at your AI agent's OTLP traces. Get a report on what's wasted.

```
enable telemetry  →  point at the collector output  →  redundo adapt  →  redundo analyze  →  HTML report
```

```bash
redundo adapt --source openinference ./otlp_traces | redundo analyze --format html > report.html
```

No account, no SaaS, no infrastructure to stand up beyond a local OTLP
receiver. It runs entirely on your machine, over data that never leaves it.

## See it work

```bash
redundo analyze examples/demo_trace.jsonl
```

```
Coverage: 16/25 events priced (64%) -- $0.1060 of tracked spend is what this analysis actually covers.
  9 event(s) had no cost_usd and are excluded from every dollar figure below -- the percentages are computed on the priced subset, not your total spend.

Candidate redundant-repeat pairs: 8

3 confirmed_waste -- repeated call, unchanged result, no intervening write, task failed. All four, confirmed -- drop any one and it's a guess, not a finding.
  cost_usd:   0.028000
  by model:
    gpt-5.6: count=3 cost_usd=0.028000 tokens_in=3240 tokens_out=230
  sample cases (spot-check these by hand):
    - task=session-001 step=2 (tool_call/web_search): result identical; no intervening write; task terminated in failure
    - task=session-006 step=1 (llm_call/gpt-5.6): result identical; no intervening write; task terminated in failure

3 likely_legitimate -- a specific reason it's not waste: result changed (polling worked), a write intervened (verification), or the task succeeded and neither the result nor the write status is already confirmed waste on its own
2 unclassified -- everything else -- a required signal (result, write status, or outcome) was missing from the trace, or the call confirms waste on its own and task-level success can't settle whether it mattered. No verdict, on purpose
0 near_duplicate -- arguments are similar but not identical to an earlier call on the same execution path (SimHash fingerprint comparison, not exact content_hash equality) -- surfaced for manual review, not a waste/legitimate verdict.
```

Every number above traces back to a `sample case` you can check by hand
against the fixture at [`examples/demo_trace.jsonl`](examples/demo_trace.jsonl).
Or open [`examples/demo_report.html`](examples/demo_report.html) for the
same result as the self-contained HTML report. That's the actual product:
not a dollar figure to trust blindly, but one you can verify line by line.

## What it is

`redundo` is two programs joined by a pipe, installed as one package.
**`adapt`** turns OTLP telemetry from a specific agent framework (Hermes,
Claude Code, Claude Cowork, ...) into one common, well-defined event
schema. **`analyze`** reads that schema and runs an actual analysis over
it. Today that means classifying repeated LLM/tool calls as **confirmed
waste**, **likely legitimate**, or **unclassified**, plus a fourth,
lower-confidence **near-duplicate** bucket for similar-but-not-identical
repeats. The schema in between is the real contract, not the pipe. If
your framework isn't supported yet, skip `adapt` and pipe in your own
NDJSON matching [the schema](docs/schema.md); `analyze` doesn't know or
care where its input came from.

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
redundo` without the extra. `adapt` and `analyze` themselves have no
dependencies at all; only `collect` needs the extra.

The source (which framework produced the captured data) is detected
automatically. You don't tell it. `--summary` on `adapt` prints what it
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
| **OpenClaw** (`@openclaw/diagnostics-otel`) | Content is opt-in (`captureContent`, off by default). `cost_usd` is an estimate apportioned from the metrics signal, only when metrics were captured. `task_id` is always trace-scoped, not conversation-scoped; see docs/openclaw.md for why that's structural, not a fallback | [docs/openclaw.md](docs/openclaw.md) |
| **OpenClaw Localtrace** ([openclaw-localtrace](https://github.com/CogentWizards/openclaw-localtrace) plugin) | Real conversation-scoped `task_id`, a real `write`/mutation signal per tool call, and a per-call `cost_usd` estimate. Capabilities no other OpenClaw source here has | [docs/openclaw-localtrace.md](docs/openclaw-localtrace.md) |

More sources are expected over time. An OpenTelemetry-based agent
observability adapter is only useful if it keeps pace with what people are
actually building agents with. **Third-party sources don't need a PR
here at all.** Install a package registering itself under the
`redundo.adapter.sources` entry-point group and it appears in `--source`
automatically; see [docs/plugins.md](docs/plugins.md).

## Why trust these numbers

Every report opens with a coverage line, before any bucket:

```
Coverage: 16/25 events priced (64%) -- $0.1060 of tracked spend is what this analysis actually covers.
  9 event(s) had no cost_usd and are excluded from every dollar figure below -- the percentages are computed on the priced subset, not your total spend.
```

This is measured over the *entire loaded corpus*, not just the events
that ended up in a candidate pair. The point is telling a reader what
fraction of their total data the numbers below are even computed on,
before they trust or forward those numbers. If `metadata.task_id_source`
is present on any event, a second line reports what fraction were
grouped by a source's most precise available signal versus a fallback.
If no source in the loaded corpus ever sets that key, the line is
omitted entirely rather than reporting a fabricated "0%": silence here
means "this dimension can't be spoken to for this data," not "everything
failed."

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

Five specific edge cases in the schema contract itself, like what
happens with no `parent_id` and how branching, parallel tasks, and
chains of repeats get handled, are resolved explicitly, not left
ambiguous. See [docs/schema.md](docs/schema.md) for exactly what each
one decides and why.

## The four buckets

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
  buckets above. See [docs/hashing.md](docs/hashing.md) for what a
  similarity fingerprint can and can't support.

The rule itself is printed next to every count in the actual report
output, not left implicit in a label. `"42 confirmed_waste"` is a claim;
`"42 confirmed_waste -- repeated call, unchanged result, no intervening
write, task failed"` is a claim someone can check against one case by
hand. `unclassified` is not minimized with heuristics: a confident wrong
classification is worse than a large unclassified bucket, because the
first time someone spot-checks a "confirmed waste" case by hand and finds
it wasn't, the tool stops being trusted.

There's a fifth bucket this analysis deliberately doesn't attempt:
**silent-wrong** (identical call, identical-looking success, wrong answer
both times). That's not computable from a trace alone. It needs a
correctness oracle external to the trace itself. A different analysis
module, built on the same schema, is where something like that would live.

## Setup by source

Every source below is the same three-step shape from the Quickstart:
start `redundo collect`, point the framework's OTLP export at it, run
the framework, then `redundo adapt | redundo analyze`. What differs per
source is exactly which env vars and config keys turn that export on and
what they unlock. That's what each subsection gives you, copy-pasteable.
Start the collector once, in its own terminal, before any of these:

```bash
pip install "redundo[collector]"
redundo collect --out-dir ./otlp_traces &
```

### OpenClaw

```bash
openclaw plugins install clawhub:@openclaw/diagnostics-otel
openclaw plugins enable diagnostics-otel
openclaw config set diagnostics.enabled true
openclaw config set diagnostics.otel.enabled true
openclaw config set diagnostics.otel.endpoint "http://localhost:4318"
openclaw config set diagnostics.otel.captureContent true   # opt-in; off by default

# restart the Gateway, then drive real turns through it, then:
redundo adapt ./otlp_traces --source openclaw --summary | redundo analyze --format html > report.html
```

`captureContent` is opt-in and off by default. Without it you still get
counts, timing, and an estimated cost, but call/result content stays
unobservable, so nothing can be confirmed as a repeat. Cost only exists
on the metrics signal (never per-call), so it's an apportioned estimate,
not an exactly metered figure. `cost_usd` stays `None` entirely unless
you also point `redundo adapt` at a directory that has metrics files in
it (the same `otlp_traces` dir `redundo collect` already writes them
to). `task_id` for this source is always trace-scoped, not
conversation-scoped, which is a structural property of what OpenClaw's
exporter emits, not a fallback. Full detail, including a documented case
where a live Gateway exported zero spans across several real turns (an
OpenClaw-side gap, not a redundo one), is in
[docs/openclaw.md](docs/openclaw.md).

Want a real conversation-scoped `task_id`, a real per-call write signal,
and a real per-call cost estimate instead of an apportioned one? See
[docs/openclaw-localtrace.md](docs/openclaw-localtrace.md) for the
[openclaw-localtrace](https://github.com/CogentWizards/openclaw-localtrace)
plugin, which needs no `redundo collect` step at all.

### Hermes

Hermes (and any other framework instrumented with an OpenInference-compatible
library) doesn't need source-specific flags to unlock content the way
Claude Code or OpenClaw do. An OpenInference `LLM`/`TOOL` span carries its
full `input.value`/`output.value` by default. All that's needed is
standard OTel export, pointed at the collector:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
# then run Hermes (or your OpenInference-instrumented app) as usual
```

```bash
redundo adapt ./otlp_traces --source openinference --summary | redundo analyze --format html > report.html
```

`task_id` prefers `gen_ai.conversation.id`. If a trace's spans never
carry it, grouping falls back to the trace ID and that fallback is
reported, not silently assumed. See
[docs/openinference.md](docs/openinference.md).

### Claude CLI

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none   # not consumed by this adapter
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_TRACES_EXPORT_INTERVAL=1000
export OTEL_LOGS_EXPORT_INTERVAL=1000
export OTEL_LOG_USER_PROMPTS=1      # first-of-turn llm_call content
export OTEL_LOG_TOOL_DETAILS=1      # built-in tool arguments + MCP tool_input
export OTEL_LOG_TOOL_CONTENT=1      # tool.output content (needs tracing on)

# run your claude session(s), then:
redundo adapt ./otlp_traces --source claude-code --summary | redundo analyze --format html > report.html
```

Two independent signals matter here. Traces alone still produce a valid
corpus, but MCP tool call *arguments* only ever appear on the logs signal
(`OTEL_LOGS_EXPORTER=otlp` + `OTEL_LOG_TOOL_DETAILS=1`), and tool
*output* content only ever appears in a span event gated by
`OTEL_LOG_TOOL_CONTENT=1`. `OTEL_TRACES_EXPORT_INTERVAL=1000` (or lower)
matters for short-lived `-p` invocations: the default 5s interval can
lose the whole session to an early exit. Full detail in
[docs/claude-code.md](docs/claude-code.md).

### Claude Agent SDK

The same env vars as Claude CLI above. The SDK launches the `claude`
binary as a subprocess and that subprocess inherits its parent's
environment, so set these in whatever process calls `query()` (shell
`export` before running your script, or `os.environ`/`process.env` before
the SDK import) rather than anywhere inside the SDK's own options:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_TRACES_EXPORT_INTERVAL=1000
export OTEL_LOGS_EXPORT_INTERVAL=1000
export OTEL_LOG_USER_PROMPTS=1
export OTEL_LOG_TOOL_DETAILS=1
export OTEL_LOG_TOOL_CONTENT=1

python your_agent_script.py   # anything that calls claude_agent_sdk.query()
```

```bash
redundo adapt ./otlp_traces --source claude-code --summary | redundo analyze --format html > report.html
```

Same `--source claude-code` as the CLI. The SDK is detected as the same
source, not a separate one. One thing genuinely differs under the hood:
the SDK always launches the CLI in streaming mode, which never emits the
`claude_code.interaction` span the CLI normally uses to attach a turn's
prompt text. This adapter recovers `llm_call` content for that case
automatically via a time-window correlation against the logs signal.
There's nothing to configure for it, but if you want to know exactly how
(and its limits), see "Recovering `llm_call` content when there's no
interaction span at all" in [docs/claude-code.md](docs/claude-code.md).

Each source has a genuinely different OTLP shape, not just different
attribute names, and `redundo adapt` tells them apart from the data
itself (span names, `openinference.span.kind` attributes, and for
logs-only sources, the OTLP resource-level `service.name` attribute).
See [`detect.py`](src/redundo/adapter/detect.py)'s module docstring for
the exact rules, and force a specific source with `--source` if you ever
need to skip detection.

## Usage

```bash
redundo analyze trace.jsonl
redundo analyze trace.jsonl --format json
redundo analyze trace.jsonl --format html --output report.html
redundo analyze trace.jsonl --lenient   # skip malformed rows instead of failing
redundo analyze trace.jsonl --analysis waste   # the default -- other analyses can register under this flag
```

The HTML report is a single self-contained file: no CDN assets, no
webfonts, no JS, just inline SVG and CSS, safe to open straight from disk
or send anywhere. Every value pulled from the trace (model names,
workflow labels, classification reasons) is HTML-escaped before being
written, since that content is attacker-controlled if the trace comes
from somewhere untrusted.

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

Or run a different analysis the same way. Any `Analysis` subclass takes
a `list[Event]` and returns an `AnalysisResult` that every renderer
(`to_text`/`to_json`/`to_html`) already knows how to display:

```python
from redundo.analyzer import AnalysisRegistry, to_html

result = AnalysisRegistry().get("waste").run(events)
open("report.html", "w").write(to_html(result))
```

Every `Classification` carries a one-line `reason` naming exactly which
signals fired and why. That's what the CLI's "sample cases" section
prints, meant for spot-checking a verdict by hand.

## Extending redundo

Adapter sources, analyses, and report formats are all plugin points via
Python entry points. A separate package registering itself under
`redundo.adapter.sources`, `redundo.analyzer.analyses`, or
`redundo.analyzer.report_formats` shows up in `--source`/`--analysis`/
`--format` automatically, no PR against this repo needed. See
[docs/plugins.md](docs/plugins.md) for the full contract and
[`examples/redundo-plugin-example/`](examples/redundo-plugin-example/)
for a complete, working, minimal package implementing all three.

Contributing one in-tree instead, or to the shared schema/coverage
infrastructure itself, is covered in [CONTRIBUTING.md](CONTRIBUTING.md).

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

MIT. See [LICENSE](LICENSE).
