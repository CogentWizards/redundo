# redundo

[![PyPI](https://img.shields.io/pypi/v/redundo.svg)](https://pypi.org/project/redundo/)
[![CI](https://github.com/CogentWizards/redundo/actions/workflows/ci.yml/badge.svg)](https://github.com/CogentWizards/redundo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/CogentWizards/redundo/blob/main/LICENSE)

Point it at your AI agent's OTLP traces. Get a report on what's actually wasted: repeated work, not a guess.

- **Runs on your machine.** No account, no SaaS. Point it at OTLP traces you already have, or capture them with the bundled collector.
- **Every number is checkable.** Each bucket links back to a real, hand-verifiable case.

## Quickstart

Already have a directory of OTLP JSON traces (your framework wrote them
directly, or you captured them some other way)? Point `redundo` at it:

```bash
pip install redundo
redundo adapt ./otlp_traces --summary | redundo analyze --format html > report.html
```

Each source has a genuinely different OTLP shape, and `redundo adapt`
tells them apart from the data itself. See [`detect.py`](https://github.com/CogentWizards/redundo/blob/main/src/redundo/adapter/detect.py)'s
module docstring for the exact rules, or force one with `--source`.

Each half also runs on its own:

```bash
redundo adapt ./otlp_traces -o trace.jsonl       # just convert
redundo analyze trace.jsonl --format json         # just analyze a file
redundo analyze trace.jsonl                       # reads stdin if the path is omitted or "-"
```

### Capturing OTLP traces

Don't have traces yet? Most frameworks only export telemetry over the
network, so you need somewhere local to catch it first:

```bash
pip install "redundo[collector]"
redundo collect --out-dir ./otlp_traces &
# enable your framework's OTLP export (see below), run it, then adapt/analyze as above
```

Setup for your framework, most need the collector above running first;
OpenClaw's own plugin below is the one exception:

<details>
<summary><strong>OpenClaw</strong></summary>

Recommended: the [openclaw-localtrace](https://github.com/CogentWizards/openclaw-localtrace)
plugin. OpenClaw's own built-in exporter (`@openclaw/diagnostics-otel`)
strips every session and write signal this analysis relies on most; the
plugin exists specifically to keep them, writing straight to a local
directory instead of a network endpoint. No `redundo collect` step for
this source.

```bash
openclaw plugins install clawhub:@cogentwizards/openclaw-localtrace
openclaw plugins enable openclaw-localtrace
openclaw config set plugins.entries.openclaw-localtrace.config.enabled true
openclaw config set plugins.entries.openclaw-localtrace.config.captureIdentifiers true   # opt-in; off by default
openclaw config set plugins.entries.openclaw-localtrace.config.captureContent true       # opt-in; off by default
openclaw config set plugins.entries.openclaw-localtrace.hooks.allowConversationAccess true
openclaw gateway restart

# drive real turns through the Gateway, then:
redundo adapt "$(openclaw config get plugins.entries.openclaw-localtrace.config.outputDir)" \
  --summary | redundo analyze --format html > report.html
```

`captureIdentifiers` and `hooks.allowConversationAccess` unlock a real
conversation-scoped `task_id` and a real per-call write signal. Read the
plugin's own README before turning them on. Without the plugin at all,
[docs/openclaw.md](https://github.com/CogentWizards/redundo/blob/main/docs/openclaw.md) covers the built-in exporter instead, at
the cost of that missing signal.

</details>

<details>
<summary><strong>Hermes</strong></summary>

Needs the community [hermes-otel](https://github.com/briancaffey/hermes-otel)
plugin installed first:

```bash
hermes plugins install briancaffey/hermes-otel/hermes_otel
# import into the SAME venv that runs `hermes`. Check `hermes --version`'s
# "Install directory" for the real path if this doesn't match yours:
/path/to/hermes-agent/venv/bin/pip install -r ~/.hermes/plugins/hermes_otel/requirements.txt
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318/v1/traces"   # note the /v1/traces suffix

# restart Hermes, drive real turns through it, then:
redundo adapt ./otlp_traces --source openinference --summary | redundo analyze --format html > report.html
```

Full content and a real conversation-scoped `task_id` both work out of
the box, no permission opt-in needed. See [docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md).

</details>

<details>
<summary><strong>OpenAI Agents SDK</strong></summary>

Instrument it with the community
[openinference-instrumentation-openai-agents](https://github.com/Arize-ai/openinference)
package, pointed at a `redundo collect` receiver:

```bash
pip install openinference-instrumentation-openai-agents opentelemetry-exporter-otlp-proto-http
```

```python
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

# run your agent(s), then:
```

```bash
redundo adapt ./otlp_traces --source openinference --summary | redundo analyze --format html > report.html
```

In-trace handoffs work out of the box. Cross-trace correlation
(the SDK's own `group_id`) is dropped by this translator today, an
upstream gap, not something this adapter can fix. See
[docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md)'s per-source table.

</details>

<details>
<summary><strong>Google ADK</strong></summary>

Instrument it with the community
[openinference-instrumentation-google-adk](https://github.com/Arize-ai/openinference)
package, pointed at a `redundo collect` receiver:

```bash
pip install openinference-instrumentation-google-adk opentelemetry-exporter-otlp-proto-http
```

```python
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
GoogleADKInstrumentor().instrument(tracer_provider=provider)

# run your agent(s), then:
```

```bash
redundo adapt ./otlp_traces --source openinference --summary | redundo analyze --format html > report.html
```

ADK's own session id maps onto the standard `session.id` attribute, so a
real conversation-scoped `task_id` works out of the box. Subagent
delegation via `AgentTool` reuses the parent's own session id, so
delegated work lands in the same task automatically, no extra link
needed. See [docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md)'s per-source
table.

</details>

<details>
<summary><strong>Claude Code (CLI)</strong></summary>

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none   # not consumed by this adapter
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_TRACES_EXPORT_INTERVAL=1000   # the default 5s can lose a short -p session to an early exit
export OTEL_LOGS_EXPORT_INTERVAL=1000
export OTEL_LOG_USER_PROMPTS=1      # first-of-turn llm_call content
export OTEL_LOG_TOOL_DETAILS=1      # built-in tool arguments + MCP tool_input
export OTEL_LOG_TOOL_CONTENT=1      # tool.output content (needs tracing on)

# run your claude session(s), then:
redundo adapt ./otlp_traces --source claude-code --summary | redundo analyze --format html > report.html
```

MCP tool call *arguments* only ever appear on the logs signal; tool
*output* content only ever appears in a span event gated by
`OTEL_LOG_TOOL_CONTENT=1`. Full detail in [docs/claude-code.md](https://github.com/CogentWizards/redundo/blob/main/docs/claude-code.md).

</details>

<details>
<summary><strong>Claude Agent SDK</strong></summary>

Same env vars as the CLI above. The SDK launches the `claude` binary as a
subprocess, which inherits its parent's environment, so set these in
whatever process calls `query()`, shell `export` before running your
script, or `os.environ`/`process.env` before the SDK import, rather than
anywhere inside the SDK's own options.

```bash
python your_agent_script.py   # anything that calls claude_agent_sdk.query()
redundo adapt ./otlp_traces --source claude-code --summary | redundo analyze --format html > report.html
```

Same `--source claude-code` as the CLI; the SDK is detected as the same
source. One thing genuinely differs: the SDK always launches the CLI in
streaming mode, which never emits the span the CLI normally uses to
attach a turn's prompt text. This adapter recovers that content
automatically via a time-window correlation against the logs signal. See
"Agent SDK content recovery" in [docs/claude-code.md](https://github.com/CogentWizards/redundo/blob/main/docs/claude-code.md).

</details>

## See it work

```bash
redundo analyze examples/demo_trace.jsonl
```

```
Coverage: 16/25 events priced (64%). $0.1060 of tracked spend is what this analysis actually covers.
  Cost basis: $0.1060 (100%) reported directly by the source.

Candidate redundant-repeat pairs: 8

Fix these first:
  1. $0.0200 for task=session-006 step=1 (llm_call/gpt-5.6): result identical; no intervening write; task terminated in failure

3 confirmed_waste: repeated call, unchanged result, no intervening write, task failed
  Your agent repeated itself, learned nothing new, and still failed: these are the places it was stuck, not working.
  at 1,000 calls/day: ~$52.50/mo projected
3 likely_legitimate: result changed, or a write intervened
2 unclassified: a required signal was missing from the trace, no verdict, on purpose
0 near_duplicate / 0 cross_task_redundancy / 0 recurring_pattern
```

Every count above traces back to a real, checkable case in
[`examples/demo_trace.jsonl`](https://github.com/CogentWizards/redundo/blob/main/examples/demo_trace.jsonl). Full report:
[`examples/demo_report.html`](https://github.com/CogentWizards/redundo/blob/main/examples/demo_report.html). Three live,
narrated demo apps (Claude Agent SDK, OpenClaw, Hermes): [`examples/demo-apps/`](https://github.com/CogentWizards/redundo/tree/main/examples/demo-apps/).

## How it works

`redundo` is two programs joined by a plain schema, installed as one package.

**`adapt`** turns a framework's OTLP telemetry into one common event schema ([docs/schema.md](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md)). **`analyze`** classifies repeated calls into one of six buckets:

| Bucket | Fires when |
|---|---|
| `confirmed_waste` | identical call, identical result, no write in between, task failed |
| `likely_legitimate` | the result changed, or a write intervened |
| `unclassified` | a required signal was missing from the trace |
| `near_duplicate` | similar, not identical, surfaced for review, not scored |
| `cross_task_redundancy` | same call, different task, a *confirmed* link between them |
| `recurring_pattern` | same call, different task, no confirmed link |

Full reasoning for each: [docs/schema.md](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md#the-six-buckets), or [`classify.py`](https://github.com/CogentWizards/redundo/blob/main/src/redundo/analyzer/classify.py)'s own module docstring.

## Not a tracing platform

Langfuse, Phoenix, and other observability platforms show you traces:
spans, timings, a UI to browse them. That's real, valuable, and a
different job. `redundo` doesn't show you traces, it adjudicates them.
For every repeated call it finds, it issues one of the six verdicts
above under a stated evidence rule, never a vague severity score. When
the trace doesn't carry enough signal to decide, it says so directly:
`unclassified` is a verdict too, not a fallback dressed up as an
answer. Nobody else ships that abstention. The two aren't competitors:
point one of those platforms' own OTLP export at `redundo` instead of
choosing between them. One shows you what happened; the other tells
you which of it was wasted.

## Modular by design

`redundo` is three small programs that plug together, not one monolith:

- **`collect`** is a local OTLP receiver. It writes whatever telemetry it's sent to disk, nothing more.
- **`adapt`** turns one framework's raw telemetry into the common event schema.
- **`analyze`** classifies repeated calls from that schema into the six buckets above.

Each stage only needs the one before it to speak the schema in between,
so any stage can be swapped for your own. Bring your own event source
by writing an `adapt` plugin, or add a new classification by writing an
`analyze` plugin, no fork or PR against this repo required. See
[docs/plugins.md](https://github.com/CogentWizards/redundo/blob/main/docs/plugins.md).

## Supported sources

| Source | Docs |
|---|---|
| Hermes, and anything OpenInference-instrumented | [docs/openinference.md](https://github.com/CogentWizards/redundo/blob/main/docs/openinference.md) |
| Claude Code (CLI, IDE extensions, Agent SDK) | [docs/claude-code.md](https://github.com/CogentWizards/redundo/blob/main/docs/claude-code.md) |
| Claude Cowork | [docs/cowork.md](https://github.com/CogentWizards/redundo/blob/main/docs/cowork.md) |
| OpenClaw | [docs/openclaw.md](https://github.com/CogentWizards/redundo/blob/main/docs/openclaw.md), or the [openclaw-localtrace](https://github.com/CogentWizards/openclaw-localtrace) plugin ([docs](https://github.com/CogentWizards/redundo/blob/main/docs/openclaw-localtrace.md)) for a real write signal |

Not listed? Pipe your own NDJSON matching [the schema](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md)
straight into `analyze`. It doesn't know or care where its input came
from. Or write an adapter plugin, no PR against this repo required: [docs/plugins.md](https://github.com/CogentWizards/redundo/blob/main/docs/plugins.md).

## Pricing data

When a source doesn't report `cost_usd` directly, `redundo` estimates it
from a bundled, per-model pricing snapshot. Refresh it any time:

```bash
redundo update-pricing
```

Full detail: [docs/pricing.md](https://github.com/CogentWizards/redundo/blob/main/docs/pricing.md).

## Docs

- [docs/schema.md](https://github.com/CogentWizards/redundo/blob/main/docs/schema.md): the event schema and the six buckets, in full
- [docs/hashing.md](https://github.com/CogentWizards/redundo/blob/main/docs/hashing.md): content hashing and similarity fingerprinting
- [docs/pricing.md](https://github.com/CogentWizards/redundo/blob/main/docs/pricing.md): how cost is estimated, and how to refresh it
- [docs/context-drift.md](https://github.com/CogentWizards/redundo/blob/main/docs/context-drift.md): `redundo drift`, a separate heuristic
- [docs/plugins.md](https://github.com/CogentWizards/redundo/blob/main/docs/plugins.md): writing your own source, analysis, or report format

## Development

```bash
uv sync
uv run pytest
```

Contributions welcome. See [CONTRIBUTING.md](https://github.com/CogentWizards/redundo/blob/main/CONTRIBUTING.md).

## License

MIT. See [LICENSE](https://github.com/CogentWizards/redundo/blob/main/LICENSE).
