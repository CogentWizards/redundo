# Per-framework quickstart

How to turn on OTLP export for each supported framework, then feed the
result to `redundo adapt | redundo analyze`. See the [README](../README.md)
for what each source actually captures, and the docs linked from each
section below for the full adapter contract.

Most sources need a local OTLP receiver running before you turn on
export:

```bash
redundo collect --out-dir ./otlp_traces &
```

OpenClaw via the `openclaw-localtrace` plugin is the one exception: it
writes trace files directly and needs no collector.

## OpenClaw

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
conversation-scoped `task_id` and a real per-call write signal — read the
plugin's own README before turning them on. Without the plugin at all,
[docs/openclaw.md](openclaw.md) covers the built-in exporter instead, at
the cost of that missing signal.

## Hermes

Needs the community [hermes-otel](https://github.com/briancaffey/hermes-otel)
plugin installed first:

```bash
hermes plugins install briancaffey/hermes-otel/hermes_otel
# import into the SAME venv that runs `hermes` -- check `hermes --version`'s
# "Install directory" for the real path if this doesn't match yours:
/path/to/hermes-agent/venv/bin/pip install -r ~/.hermes/plugins/hermes_otel/requirements.txt
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318/v1/traces"   # note the /v1/traces suffix

# restart Hermes, drive real turns through it, then:
redundo adapt ./otlp_traces --source openinference --summary | redundo analyze --format html > report.html
```

Full content and a real conversation-scoped `task_id` both work out of
the box, no permission opt-in needed. See [docs/openinference.md](openinference.md).

## Claude Code (CLI)

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
`OTEL_LOG_TOOL_CONTENT=1`. Full detail in [docs/claude-code.md](claude-code.md).

## Claude Agent SDK

Same env vars as the CLI above. The SDK launches the `claude` binary as a
subprocess, which inherits its parent's environment, so set these in
whatever process calls `query()` — shell `export` before running your
script, or `os.environ`/`process.env` before the SDK import — rather than
anywhere inside the SDK's own options.

```bash
python your_agent_script.py   # anything that calls claude_agent_sdk.query()
redundo adapt ./otlp_traces --source claude-code --summary | redundo analyze --format html > report.html
```

Same `--source claude-code` as the CLI; the SDK is detected as the same
source. One thing genuinely differs: the SDK always launches the CLI in
streaming mode, which never emits the span the CLI normally uses to
attach a turn's prompt text. This adapter recovers that content
automatically via a time-window correlation against the logs signal — see
"Recovering `llm_call` content when there's no interaction span at all"
in [docs/claude-code.md](claude-code.md).

## Detection and manual overrides

Each source has a genuinely different OTLP shape, and `redundo adapt`
tells them apart from the data itself — see [`detect.py`](../src/redundo/adapter/detect.py)'s
module docstring for the exact rules. Force a specific source with
`--source` if you ever need to skip detection.

Each half also runs on its own:

```bash
redundo adapt ./otlp_traces -o trace.jsonl       # just convert
redundo analyze trace.jsonl --format json         # just analyze a file
redundo analyze trace.jsonl                       # reads stdin if the path is omitted or "-"
```
