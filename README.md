# redundo

[![PyPI](https://img.shields.io/pypi/v/redundo.svg)](https://pypi.org/project/redundo/)
[![CI](https://github.com/CogentWizards/redundo/actions/workflows/ci.yml/badge.svg)](https://github.com/CogentWizards/redundo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Point it at your AI agent's OTLP traces. Get a report on what's actually wasted — repeated work, not a guess.

- **Never guesses.** A missing signal stays `unclassified`, not a heuristic dressed up as a finding.
- **Runs on your machine.** No account, no SaaS, nothing to stand up but a local OTLP receiver.
- **Every number is checkable.** Each bucket links back to a real, hand-verifiable case.

## Quickstart

```bash
pip install "redundo[collector]"
redundo collect --out-dir ./otlp_traces &
# enable your framework's OTLP export, run it, then:
redundo adapt ./otlp_traces --summary | redundo analyze --format html > report.html
```

Per-framework setup (OpenClaw, Hermes, Claude Code, Claude Agent SDK): [docs/quickstart.md](docs/quickstart.md).

## See it work

```bash
redundo analyze examples/demo_trace.jsonl
```

```
Coverage: 16/25 events priced (64%). $0.1060 of tracked spend is what this analysis actually covers.

Candidate redundant-repeat pairs: 8

3 confirmed_waste: repeated call, unchanged result, no intervening write, task failed
3 likely_legitimate: result changed, or a write intervened
2 unclassified: a required signal was missing from the trace -- no verdict, on purpose
0 near_duplicate / 0 cross_task_redundancy / 0 recurring_pattern
```

Every count above traces back to a real, checkable case in
[`examples/demo_trace.jsonl`](examples/demo_trace.jsonl). Full report:
[`examples/demo_report.html`](examples/demo_report.html). Three live,
narrated demo apps (Claude Agent SDK, OpenClaw, Hermes): [`examples/demo-apps/`](examples/demo-apps/).

## How it works

`redundo` is two programs joined by a plain schema, installed as one package.

**`adapt`** turns a framework's OTLP telemetry into one common event schema ([docs/schema.md](docs/schema.md)). **`analyze`** classifies repeated calls into one of six buckets:

| Bucket | Fires when |
|---|---|
| `confirmed_waste` | identical call, identical result, no write in between, task failed |
| `likely_legitimate` | the result changed, or a write intervened |
| `unclassified` | a required signal was missing from the trace |
| `near_duplicate` | similar, not identical — surfaced for review, not scored |
| `cross_task_redundancy` | same call, different task, a *confirmed* link between them |
| `recurring_pattern` | same call, different task, no confirmed link — a coincidence, not a claim |

Full reasoning for each: [docs/schema.md](docs/schema.md#the-six-buckets), or [`classify.py`](src/redundo/analyzer/classify.py)'s own module docstring.

## Supported sources

| Source | Docs |
|---|---|
| Hermes, and anything OpenInference-instrumented | [docs/openinference.md](docs/openinference.md) |
| Claude Code (CLI, IDE extensions, Agent SDK) | [docs/claude-code.md](docs/claude-code.md) |
| Claude Cowork | [docs/cowork.md](docs/cowork.md) |
| OpenClaw | [docs/openclaw.md](docs/openclaw.md), or the [openclaw-localtrace](https://github.com/CogentWizards/openclaw-localtrace) plugin ([docs](docs/openclaw-localtrace.md)) for a real write signal |

Not listed? Pipe your own NDJSON matching [the schema](docs/schema.md)
straight into `analyze` — it doesn't know or care where its input came
from. Or write an adapter plugin, no PR against this repo required: [docs/plugins.md](docs/plugins.md).

## Docs

- [docs/schema.md](docs/schema.md) — the event schema and the six buckets, in full
- [docs/quickstart.md](docs/quickstart.md) — per-framework telemetry setup
- [docs/hashing.md](docs/hashing.md) — content hashing and similarity fingerprinting
- [docs/context-drift.md](docs/context-drift.md) — `redundo drift`, a separate heuristic
- [docs/plugins.md](docs/plugins.md) — writing your own source, analysis, or report format

## Development

```bash
uv sync
uv run pytest
```

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
