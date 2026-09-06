# Writing a plugin

redundo's three stages -- adapter sources, analyses, report formats --
are all extensible via ordinary Python packaging: register a class or
function under one of three entry-point groups, and it shows up
alongside the built-ins with no changes to redundo itself. This doc is
the contract for each; [`examples/redundo-plugin-example/`](../examples/redundo-plugin-example/)
is a complete, working, minimal package implementing all three, and
[`tests/test_plugin_end_to_end.py`](../tests/test_plugin_end_to_end.py)
proves it actually works through the real CLI, not just that the code
imports.

The built-in sources/analyses/formats go through the *identical* path --
this repo's own `pyproject.toml` registers them the same way a
third-party package would. Nothing about being "built-in" is special.

## Adapter sources -- `redundo.adapter.sources`

```python
from redundo.adapter import AdapterSource, Detection

class MySource(AdapterSource):
    name = "my-source"  # what shows up in --source's choices

    def detect(self, documents: list[dict]) -> Detection | None:
        """documents: parsed OTLP JSON export documents (a mix of trace and
        log documents, in any order). Return a Detection if something in
        the data itself reliably identifies your source; None if it
        doesn't recognize this corpus. Never raise -- "I don't know" is a
        valid answer here, not a failure.
        """
        ...

    def convert(self, documents: list[dict]) -> tuple[list[dict], object]:
        """The full, unfiltered document list every time -- filter to
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

That's it -- `pip install` (or an editable install, for local development)
this package and `my-source` appears in `redundo adapt --source`'s
choices, and in auto-detection if `detect()` returns non-`None` for a
real corpus.

## Analyses -- `redundo.analyzer.analyses`

```python
from redundo.analyzer import Analysis, AnalysisResult, Bucket, Slice
from redundo.analyzer.metrics import compute_generic_coverage

class MyAnalysis(Analysis):
    name = "my-analysis"  # what shows up in --analysis's choices

    def run(self, events: list[Event]) -> AnalysisResult:
        """The whole analysis: whatever you want to compute over `events`,
        packaged into buckets. There's no fixed enum to match -- Bucket.key
        is just a string, and you decide how many buckets make sense.
        """
        coverage = compute_generic_coverage(events)
        # ... your own logic; compute_generic_coverage() gives you the two
        # dimensions every analysis can speak to (pricing, task-id
        # confidence) for free. Append your own coverage caveats as plain
        # sentences to coverage.extra_notes -- don't invent new
        # CoverageStats fields for your analysis's own vocabulary.
        return AnalysisResult(
            coverage=coverage,
            buckets=[Bucket(key="...", label="...", rule_text="...", slice=Slice())],
            total_candidates=...,
            analysis_name=self.name,
        )
```

A conforming `AnalysisResult` gets `to_text`/`to_json`/`to_html` rendering
for free -- you don't write any rendering code at all unless you want a
different format too (see below). If your analysis's constructor takes
keyword arguments (like `WasteAnalysis(keep_reasons=...)`), the CLI passes
`keep_reasons=` through when present and falls back to no arguments on
`TypeError` -- not every analysis needs that particular knob.

Register it:

```toml
[project.entry-points."redundo.analyzer.analyses"]
my-analysis = "my_package.analysis:MyAnalysis"
```

## Report formats -- `redundo.analyzer.report_formats`

```python
from redundo.analyzer import AnalysisResult

def to_my_format(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    """Render an AnalysisResult -- any analysis's, not just one you wrote --
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

## Why entry points, and why no plugin machinery beyond this

Nothing here is redundo-specific infrastructure -- `importlib.metadata`
entry points are how `pytest`, `flake8`, and most of the Python packaging
ecosystem already do this, and they're stdlib. `adapt`/`analyze` stay at
`dependencies = []`; discovering plugins costs nothing extra to depend on.

You don't need any of this to customize redundo for a one-off, either --
every piece here is a plain class or function, importable and callable
directly. `WasteAnalysis().run(events)` works with zero entry points
involved; registering one just makes your source/analysis/format
discoverable by name (`--source`, `--analysis`, `--format`) for anyone
who installs your package, instead of requiring them to write Python glue
themselves.
