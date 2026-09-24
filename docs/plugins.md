# Writing a plugin

redundo's three stages, adapter sources, analyses, report formats,
are all extensible via ordinary Python packaging: register a class or
function under one of three entry-point groups, and it shows up
alongside the built-ins with no changes to redundo itself. This doc is
the contract for each; [`examples/redundo-plugin-example/`](../examples/redundo-plugin-example/)
is a complete, working, minimal package implementing all three, and
[`tests/test_plugin_end_to_end.py`](../tests/test_plugin_end_to_end.py)
proves it actually works through the real CLI, not just that the code
imports.

The built-in sources/analyses/formats go through the *identical* path.
This repo's own `pyproject.toml` registers them the same way a
third-party package would. Nothing about being "built-in" is special.

## Adapter sources

```python
from redundo.adapter import AdapterSource, Detection

class MySource(AdapterSource):
    name = "my-source"  # what shows up in --source's choices

    def detect(self, documents: list[dict]) -> Detection | None:
        """documents: parsed OTLP JSON export documents (a mix of trace and
        log documents, in any order). Return a Detection if something in
        the data itself reliably identifies your source; None if it
        doesn't recognize this corpus. Never raise. "I don't know" is a
        valid answer here, not a failure.
        """
        ...

    def convert(self, documents: list[dict]) -> tuple[list[dict], object]:
        """The full, unfiltered document list every time. Filter to
        whichever subset you need yourself (see redundo.adapter.otlp's
        is_trace_document/is_log_document). Returns (records, summary):
        records are plain dicts matching the schema contract (see
        README.md's "The schema contract" section); summary is anything
        with a .notes() -> list[str] method, printed under --summary.
        """
        ...
```

Register it:

```toml
[project.entry-points."redundo.adapter.sources"]
my-source = "my_package.source:MySource"
```

That's it. `pip install` (or an editable install, for local development)
this package and `my-source` appears in `redundo adapt --source`'s
choices, and in auto-detection if `detect()` returns non-`None` for a
real corpus.

## Analyses

```python
from redundo.analyzer import Analysis, AnalysisResult, Bucket, Slice
from redundo.analyzer.metrics import compute_generic_coverage

class MyAnalysis(Analysis):
    name = "my-analysis"  # what shows up in --analysis's choices

    def run(self, events: list[Event]) -> AnalysisResult:
        """The whole analysis: whatever you want to compute over `events`,
        packaged into buckets. There's no fixed enum to match. Bucket.key
        is just a string, and you decide how many buckets make sense.
        """
        coverage = compute_generic_coverage(events)
        # ... your own logic; compute_generic_coverage() gives you the two
        # dimensions every analysis can speak to (pricing, task-id
        # confidence) for free. Append your own coverage caveats as plain
        # sentences to coverage.extra_notes. Don't invent new
        # CoverageStats fields for your analysis's own vocabulary.
        return AnalysisResult(
            coverage=coverage,
            buckets=[Bucket(key="...", label="...", rule_text="...", slice=Slice())],
            total_candidates=...,
            analysis_name=self.name,
        )
```

A conforming `AnalysisResult` gets `to_text`/`to_json`/`to_html` rendering
for free. You don't write any rendering code at all unless you want a
different format too (see below). If your analysis's constructor takes
keyword arguments (like `WasteAnalysis(keep_reasons=...)`), the CLI passes
`keep_reasons=` through when present and falls back to no arguments on
`TypeError`. Not every analysis needs that particular knob.

A few more fields are optional on `Bucket`/`AnalysisResult`, all left
empty/`None` by default and rendered only when you populate them:

- `Bucket.insight_text`: a short, interpretive line about what one
  specific bucket's finding actually means, distinct from `rule_text`
  (the evidence rule) and `action_text` (what to do about it).
  `WasteAnalysis` sets this only on `confirmed_waste`.
- `AnalysisResult.highlights`: a "fix these first" list of pre-rendered
  strings, already ranked in the order you want them read. Populate
  this only when you have a real, defensible way to rank (never a
  projection or a guess); leave it empty otherwise; report.py never
  invents an order for you.
- `AnalysisResult.confidence_stat`: a pre-rendered `(label, value, sub)`
  headline stat, opaque to report.py the same way as the two above. When
  present, it replaces the generic, pricing-only "Trace coverage" stat
  cell at the top of the report. Use it when your analysis has its own,
  more specific claim about how much of the trace it could actually
  speak to (`WasteAnalysis` computes "how often a repeat reached a real
  verdict" from its own three `Verdict` buckets); leave it `None` when
  there's nothing to compute one from, rather than a placeholder cell.
  `value`/`sub` are plain text, not markup: `to_html` escapes them itself,
  the same as `footnote`.

By-model/by-workflow breakdown tables also follow a shared convention:
`redundo.analyzer.metrics.UNKNOWN_MODEL_LABEL`/`UNLABELED_WORKFLOW_LABEL`
name the placeholder to use when an event has no real model/workflow
value. A breakdown that's a single row of just that placeholder gets
suppressed by report.py rather than rendered (it conveys nothing a
reader can act on); using these exact two strings is what makes your
analysis's own breakdowns get the same treatment.

Register it:

```toml
[project.entry-points."redundo.analyzer.analyses"]
my-analysis = "my_package.analysis:MyAnalysis"
```

## Report formats

```python
from redundo.analyzer import AnalysisResult

def to_my_format(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render an AnalysisResult, any analysis's, not just one you wrote,
    as a string. This is a plain function, not a class: there's no shared
    state or behavior across renderers worth a base class for.
    """
    ...
```

Register it:

```toml
[project.entry-points."redundo.analyzer.report_formats"]
my-format = "my_package.report_format:to_my_format"
```

The CLI also passes `calls_per_day=` (from `redundo analyze --calls-per-day`,
default 1000), the same optional-knob pattern as an analysis's
`keep_reasons` above: if your renderer's signature doesn't accept it,
the CLI catches the `TypeError` and calls it without that argument
instead. Only the two built-in HTML/text renderers use it today, to
scale a bucket's own dollar figure to a hypothetical monthly cost; a
format that doesn't care about the projection can simply omit the
parameter.

## Why entry points

Nothing here is redundo-specific infrastructure. `importlib.metadata`
entry points are how `pytest`, `flake8`, and most of the Python packaging
ecosystem already do this, and they're stdlib. `adapt`/`analyze` stay at
`dependencies = []`; discovering plugins costs nothing extra to depend on.

You don't need any of this to customize redundo for a one-off, either.
Every piece here is a plain class or function, importable and callable
directly. `WasteAnalysis().run(events)` works with zero entry points
involved; registering one just makes your source/analysis/format
discoverable by name (`--source`, `--analysis`, `--format`) for anyone
who installs your package, instead of requiring them to write Python glue
themselves.
