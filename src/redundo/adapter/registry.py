"""Discover and dispatch to adapter sources -- built-in or third-party,
through the identical path. A source becomes available to `redundo adapt`
by registering under the `redundo.adapter.sources` entry-point group in
its own package's `pyproject.toml`; the four sources this project ships
with do exactly that in this repo's own `pyproject.toml`, no special-cased
built-in list anywhere in this module.

Detection asks each registered source in registration order and returns
the first hit -- this preserves this project's existing precedence
(claude-code and openclaw's unambiguous span-name/attribute checks run
before openinference's, which runs before cowork's) as long as entry
points are registered in that order (see pyproject.toml).

One piece of detection genuinely doesn't decompose into "ask each source
in turn": Cowork's detection is a residual, not a positive signature --
"this is a logs-only corpus, and every event name in it is one Cowork's
own monitoring reference documents, and nothing else already claimed it."
That can't be `CoworkSource.detect()`'s own job without either duplicating
Claude Code's positive checks inside Cowork's source module or silently
depending on call order in a way that isn't visible from CoworkSource
alone. It stays here, as the one explicit fallback step, run only after
every registered source's own `detect()` has already said no.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

from .base import AdapterSource, Detection, DetectionError
from .otlp import is_log_document, parse_log_records
from .sources.cowork import COWORK_EVENT_NAMES

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "redundo.adapter.sources"


class SourceRegistry:
    def __init__(self, *, discover: bool = True) -> None:
        self._sources: dict[str, AdapterSource] = {}
        if discover:
            self.discover_entry_points()

    def register(self, source: AdapterSource) -> None:
        if source.name in self._sources:
            raise ValueError(f"adapter source {source.name!r} is already registered")
        self._sources[source.name] = source

    def get(self, name: str) -> AdapterSource:
        try:
            return self._sources[name]
        except KeyError:
            raise ValueError(
                f"unknown adapter source {name!r} -- registered: {self.names()}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._sources)

    def discover_entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(ep.load()())
            except Exception:
                logger.exception(
                    "failed to load adapter source plugin %r (%s)", ep.name, ep.value
                )

    def detect(self, documents: list[dict[str, Any]]) -> Detection:
        for source in self._sources.values():
            hit = source.detect(documents)
            if hit is not None:
                return hit

        # Cowork's residual fallback -- see module docstring.
        log_docs = [d for d in documents if is_log_document(d)]
        event_names: set[str] = set()
        for doc in log_docs:
            for rec in parse_log_records(doc):
                name = rec.attributes.get("event.name")
                if isinstance(name, str):
                    event_names.add(name)
        if event_names and event_names <= COWORK_EVENT_NAMES:
            return Detection(
                "cowork",
                "logs-only corpus with only Cowork-documented event names, no "
                "service.name and no Claude-Code-only event present",
            )

        trace_count = sum(1 for d in documents if not is_log_document(d))
        raise DetectionError(
            "could not determine which source produced this corpus -- found "
            f"{trace_count} trace document(s) and {len(log_docs)} log document(s), "
            "but none carried a recognizable span name, openinference.span.kind "
            "attribute, resource service.name, or logs-signal event.name. Pass "
            f"--source explicitly ({', '.join(self.names())})."
        )


default_registry = SourceRegistry()
