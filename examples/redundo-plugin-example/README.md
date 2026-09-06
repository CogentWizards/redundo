# redundo-plugin-example

A real, working, minimal example of all three redundo extension points --
built to back up [`docs/plugins.md`](../../docs/plugins.md), not to be a
template you'd actually ship. Each half is deliberately as small as
possible:

| Extension point | This package's example | Registered as |
|---|---|---|
| Adapter source | `ExampleSource` recognizes one made-up span name (`example.event`) and converts it into one `tool_call` record per span | `example` |
| Analysis | `CountAnalysis` produces one bucket: the total event count, no classification | `count` |
| Report format | `to_csv` renders one CSV row per bucket | `csv` |

## Try it

```bash
pip install -e . redundo
redundo adapt --help     # "example" appears in --source's choices
redundo analyze --help   # "count" appears in --analysis's choices, "csv" in --format's
```

## How this was built

Three files, one per extension point (`source.py`, `analysis.py`,
`report_format.py`), and one `pyproject.toml` registering each under its
own entry-point group:

```toml
[project.entry-points."redundo.adapter.sources"]
example = "redundo_plugin_example.source:ExampleSource"

[project.entry-points."redundo.analyzer.analyses"]
count = "redundo_plugin_example.analysis:CountAnalysis"

[project.entry-points."redundo.analyzer.report_formats"]
csv = "redundo_plugin_example.report_format:to_csv"
```

That's the whole mechanism -- no redundo source code changes, no PR
against the redundo repo. `pip install`-ing (or, as here, an editable
install) this package is what makes `example`/`count`/`csv` show up
alongside redundo's own built-ins.
