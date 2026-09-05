# Contributing

By participating in this project you agree to abide by its
[Code of Conduct](CODE_OF_CONDUCT.md).

This is two packages under one name: `redundo.adapter` turns a
specific trace source into the schema, `redundo.analyzer` runs
analyses over that schema. They share the schema contract and nothing
else at runtime — contribute to either independently.

## Workflow

- **Every change goes through a branch and a pull request against
  `main`** — including maintainers' own changes. `main` is protected:
  direct pushes are rejected, and CI must pass before a PR can merge.
- **Branch names**: `fix/…` for bug fixes, `feat/…` for new sources or
  analyses, `chore/…` for anything else (docs, CI, tooling).
- **Bug reports and feature requests** go through the issue templates
  (`.github/ISSUE_TEMPLATE/`) rather than an ad-hoc issue — the bug
  report template in particular asks for a minimal reproduction (a
  small fixture, not a real capture) and the redundo version, which is
  most of what's needed to act on it.
- **CI** (`.github/workflows/ci.yml`) runs the full test suite on
  Python 3.10–3.12 for every PR. A PR that doesn't pass CI doesn't get
  merged, no exception for "it's just docs" — the workflow itself is
  the only thing verifying that.

## Adding a new source (`redundo.adapter`)

Each source lives in its own module under `src/redundo/adapter/sources/`.
A good addition:

1. **Captures real telemetry first.** Every existing source's `docs/*.md`
   was written against actual captured OTLP JSON, not just the source's
   published documentation — several documented behaviors turned out to
   be wrong or incomplete once checked against real data. Do the same:
   enable the source's telemetry, point it at a local collector (see
   `docs/` for examples), and look at what actually comes out before
   writing conversion logic against it.
2. **Writes a `docs/<source>.md`** covering: how task/session grouping
   works, what content is and isn't observable (and under which flags),
   how tool calls and their results are represented, and a "known gaps"
   section stating plainly what can't be recovered and why. Copy the
   structure of an existing source doc.
3. **Adds a detection rule** in `detect.py` — something in the data
   itself (a span name, an attribute, a resource attribute) that
   reliably distinguishes this source from every other one already
   supported. If nothing does, say so in the docstring and require
   `--source` instead of guessing.
4. **Never fabricates content.** If a result or a piece of content isn't
   observable, omit the record or mark it explicitly rather than
   inventing a placeholder hash — see `hashing.py`'s module docstring and
   any existing source's handling of unobservable content for why: a
   placeholder hash that happens to match (or never match) another
   record's hash reads as a real finding to everything downstream.
5. **Tests against hand-built fixtures**, not just real captures (which
   are convenient for validation but too large/brittle to check in) --
   see `tests/adapter/helpers.py` for the fixture builders every existing
   source's tests use.

## Adding a new analysis (`redundo.analyzer`)

The schema and ingest layer (`schema.py`, `ingest.py`) and the reporting
infrastructure (`report.py`'s coverage plumbing, `to_text()`/`to_json()`/
`to_html()`) are not specific to waste detection. `classify.py`,
`cycles.py`, and the candidate-pair parts of `lineage.py` are — that's
today's one analysis, not the whole package.

A new analysis over the same schema should:

1. **Reuse `load_events()` and the `Event` dataclass** — never invent a
   parallel ingestion path. The schema is the contract every adapter
   targets; a second analysis reading something else defeats the point
   of having one.
2. **Report coverage honestly**, the same way the waste analysis does:
   what fraction of the loaded corpus this analysis can actually speak
   to, before any headline number. See `metrics.py`'s `CoverageStats`
   and `report.py`'s coverage lines for the pattern to follow.
3. **Never guess past a genuine unknown.** `classify.py`'s three-signal
   model (waste-supporting / legit-supporting / unknown, never a forced
   binary) and its module docstring are the reference for why: a
   confident wrong answer is worse than an honest "the trace doesn't
   say," because the first time someone spot-checks a confident answer
   by hand and finds it wrong, the tool stops being trusted.
4. **Prints its rule next to its own counts**, not just a label — see
   `RULE_TEXT` in `report.py`. A number without the rule that produced it
   isn't checkable by anyone reading the report.
5. **Ships with a fixture-based validation**, the same discipline
   `tests/analyzer/fixtures/sample.jsonl` follows: hand-built traces with
   a known, intended classification for each case your new analysis
   distinguishes, not just real captured examples (useful for demos, too
   brittle/large to be the actual test).

## Running tests

```bash
uv sync
uv run pytest
```

`tests/adapter/` and `tests/analyzer/` run independently — a change to
one package's tests should never need to touch the other's.

## Reporting a documented behavior that turned out to be wrong

If you've captured real telemetry from a source and it disagrees with
what's written in `docs/`, that's a genuinely valuable bug report even
without a fix attached — open an issue (the bug report template) with
the captured data (redacted of any real content) or a description of
the discrepancy.
