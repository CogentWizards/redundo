"""Load normalized trace events from JSONL.

This package doesn't know about MLflow, a specific harness, or any other
source -- that translation happens upstream, into the schema.Event contract.
This module just reads that contract, off disk or off an already-open
stream (stdin, most usefully -- `redundo adapt | redundo analyze`
is the whole point of having a shared schema instead of a shared file).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from .schema import Event, SchemaError


class IngestError(ValueError):
    """A row failed validation and strict mode is on."""


def _iter_lines(
    handle: TextIO,
    *,
    strict: bool,
    on_error: "list[str] | None",
) -> Iterator[Event]:
    for line_no, raw_line in enumerate(handle, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            message = f"invalid JSON at line {line_no}: {exc}"
            if strict:
                raise IngestError(message) from exc
            if on_error is not None:
                on_error.append(message)
            continue

        try:
            yield Event.from_dict(row, line_no=line_no)
        except SchemaError as exc:
            if strict:
                raise IngestError(str(exc)) from exc
            if on_error is not None:
                on_error.append(str(exc))
            continue


def iter_events(
    source: "str | Path | TextIO",
    *,
    strict: bool = True,
    on_error: "list[str] | None" = None,
) -> Iterator[Event]:
    """Yield Events from JSONL, one record per line.

    `source` is a path (str/Path), or an already-open text stream (e.g.
    sys.stdin) -- callers pass a stream when reading from a pipe rather
    than a file, so this never has to write a temp file to bridge the two.

    strict=True (default): the first malformed row raises IngestError.
    strict=False: malformed rows are skipped; if `on_error` is given, a
    description of each skipped row is appended to it instead of raised.
    Real trace data is messy -- this makes the choice explicit rather than
    silently dropping rows by default.
    """
    if hasattr(source, "read"):
        yield from _iter_lines(source, strict=strict, on_error=on_error)  # type: ignore[arg-type]
        return
    with Path(source).open(encoding="utf-8") as handle:
        yield from _iter_lines(handle, strict=strict, on_error=on_error)


def load_events(
    source: "str | Path | TextIO",
    *,
    strict: bool = True,
    on_error: "list[str] | None" = None,
) -> list[Event]:
    """Materialize iter_events into a list, sorted by (task_id, step_index).

    Sorting is load-bearing: every downstream module assumes events for a
    task arrive in step order.
    """
    events = list(iter_events(source, strict=strict, on_error=on_error))
    events.sort(key=lambda e: (e.task_id, e.step_index))
    check_consistent_hash_spec(events)
    return events


def check_consistent_hash_spec(events: list[Event]) -> str | None:
    """Refuse to proceed with a corpus that mixes content_hash procedures.

    `metadata["hash_spec"]` is a convention, not a schema field -- this
    package has no fixed opinion on what a hashing spec looks like, and
    most sources won't set it at all (0 or 1 distinct values found is
    fine, silently). But if two different values show up in the same
    corpus, two events' content_hash fields were computed by different,
    possibly incompatible procedures: an "identical hash" finding built on
    top of that would be comparing apples to a differently-normalized
    apple and calling it a match. Refuse rather than produce a confident
    wrong answer. Returns the single agreed-upon spec, or None if unset.
    """
    specs = {
        event.metadata["hash_spec"]
        for event in events
        if isinstance(event.metadata, dict) and event.metadata.get("hash_spec") is not None
    }
    if len(specs) > 1:
        raise IngestError(
            f"events use inconsistent content_hash procedures (metadata.hash_spec "
            f"values found: {sorted(specs)}) -- content_hash values are not comparable "
            "across different hash_spec versions. Re-run every source through the same "
            "adapter version, or split this corpus by hash_spec before analyzing."
        )
    return next(iter(specs), None)
