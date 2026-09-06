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
3. **Implements `AdapterSource`** (`base.py`): a `detect(documents)` that
   returns a `Detection` when something in the data itself (a span name,
   an attribute, a resource attribute) reliably distinguishes this source
   from every other one already supported, else `None` -- if nothing
   does, say so in the docstring and require `--source` instead of
   guessing; and a `convert(documents)` that can just delegate to an
   existing `convert_*()` free function. Register it under this repo's
   own `[project.entry-points."redundo.adapter.sources"]` table (built-in
   sources go through the exact same discovery path as a third-party
   plugin would -- see `registry.py`'s module docstring). A new source
   living in its own package instead of this repo needs no PR here at
   all, just its own entry point.
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

The schema and ingest layer (`schema.py`, `ingest.py`), the generic
aggregation primitives (`metrics.py`'s `Slice`/`CoverageStats`), and the
reporting infrastructure (`report.py`'s `to_text()`/`to_json()`/
`to_html()`) are not specific to waste detection — they operate on the
generic `Analysis`/`AnalysisResult`/`Bucket` shapes in `analysis.py`.
`classify.py`, `cycles.py`, and `analyses/waste.py` are the one analysis
this project ships with, not the whole package.

A new analysis over the same schema should:

1. **Implement `Analysis`** (`analysis.py`): a `run(events) -> AnalysisResult`
   that reuses `load_events()`/`Event` — never invent a parallel ingestion
   path — and produces an ordered list of `Bucket`s (whatever labels make
   sense for your analysis; there's no fixed enum to match). Conforming to
   this shape gets you all three renderers (`to_text`/`to_json`/`to_html`)
   for free — see `analyses/waste.py` for a complete example, and note
   how little of it is about rendering.
2. **Report coverage honestly**: call `metrics.compute_generic_coverage()`
   for the two dimensions every analysis can speak to (pricing, task-id
   confidence), and append any analysis-specific caveat as a pre-rendered
   sentence to `coverage.extra_notes` — see `WasteAnalysis`'s own
   candidate-pair comparability note for the pattern. Don't add a new
   field to `CoverageStats` for your analysis's own vocabulary.
3. **Never guess past a genuine unknown.** `classify.py`'s three-signal
   model (waste-supporting / legit-supporting / unknown, never a forced
   binary) and its module docstring are the reference for why: a
   confident wrong answer is worse than an honest "the trace doesn't
   say," because the first time someone spot-checks a confident answer
   by hand and finds it wrong, the tool stops being trusted.
4. **Prints its rule next to its own counts**, not just a label — each
   `Bucket.rule_text` is exactly this. A number without the rule that
   produced it isn't checkable by anyone reading the report.
5. **Ships with a fixture-based validation**, the same discipline
   `tests/analyzer/fixtures/sample.jsonl` follows: hand-built traces with
   a known, intended classification for each case your new analysis
   distinguishes, not just real captured examples (useful for demos, too
   brittle/large to be the actual test).
6. **Registers under `[project.entry-points."redundo.analyzer.analyses"]`**
   — the built-in `waste` analysis goes through the identical path (see
   `registry.py`'s module docstring). Living in its own package instead
   of this repo needs no PR here at all, just its own entry point.

## Running tests

```bash
uv sync
uv run pytest
```

`tests/adapter/` and `tests/analyzer/` run independently — a change to
one package's tests should never need to touch the other's.

## Releasing to PyPI

Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) via GitHub Actions — no API token is stored anywhere. To cut a release:

1. Bump `version` in `pyproject.toml`, merge that through the normal PR
   workflow.
2. Tag the merge commit `vX.Y.Z` and push the tag, or create a GitHub
   Release directly against `main` with that tag.
3. Publishing a GitHub Release triggers `.github/workflows/publish.yml`,
   which builds the sdist/wheel with `uv build` and publishes them via
   `pypa/gh-action-pypi-publish`.

The PyPI project must have this repository registered as a trusted
publisher first (PyPI account/org → Publishing settings → project name
`redundo`, repo `CogentWizards/redundo`, workflow `publish.yml`,
environment `pypi`) — a one-time setup step, done on pypi.org, not in
this repo.

## Reporting a documented behavior that turned out to be wrong

If you've captured real telemetry from a source and it disagrees with
what's written in `docs/`, that's a genuinely valuable bug report even
without a fix attached — open an issue (the bug report template) with
the captured data (redacted of any real content) or a description of
the discrepancy.
