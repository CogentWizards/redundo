"""redundo: adapt agent traces to a common schema, then analyze them.

    redundo adapt --source openinference traces/ | redundo analyze > report.html

`redundo.adapter` and `redundo.analyzer` are independent packages
joined by one contract, `redundo.analyzer.schema.Event` -- `analyze`
doesn't know or care whether its input came from `adapt` or was hand-built
NDJSON from a source this project doesn't support yet. See each
subpackage's own docstring for its half of the pipeline.
"""
